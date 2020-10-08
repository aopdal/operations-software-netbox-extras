#!/usr/bin/env python3
"""Generate DNS zonefile snippets with records from Netbox to be included in zonefiles.

Todo:
    * Investigate dnspython instead of doing non-abstract string manipulations.

"""
import argparse
import ipaddress
import json
import logging
import os
import shutil
import sys
import tempfile
import time

from abc import abstractmethod
from collections import defaultdict
from configparser import ConfigParser
from datetime import datetime, timedelta
from operator import attrgetter
from pathlib import Path
from typing import Any, DefaultDict, Dict, KeysView, List, Mapping, Optional, Sequence, Tuple, Union

import git
import pynetbox

from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter


logger = logging.getLogger()
GIT_USER_NAME = 'generate-dns-snippets'
GIT_USER_EMAIL = 'noc@wikimedia.org'
NO_DEVICE_NAME = 'UNASSIGNED_ADDRESSES'
# The second part is the base64 encode of 'snippets' to not easily match any existing directory prefix.
TMP_DIR_PREFIX = 'dns-c25pcHBldHM-'
EXCEPTION_RETURN_CODE = 2
ABORT_RETURN_CODE = 3
PUSH_ERROR_RETURN_CODE = 4
INVALID_SHA1_RETURN_CODE = 5
NO_CHANGES_RETURN_CODE = 99
WARNING_PERCENTAGE_LINES_CHANGED = 3
ERROR_PERCENTAGE_LINES_CHANGED = 5
WARNING_PERCENTAGE_FILES_CHANGED = 8
ERROR_PERCENTAGE_FILES_CHANGED = 15
ALLOWED_CHANGES_MINUTES = 30
ICINGA_RUN_EVERY_MINUTES = 60
ICINGA_RETRY_ON_FAILURE_MINUTES = 15

# Typing alias for a Netbox device, either physical or virtual.
NetboxDeviceType = Union[pynetbox.models.dcim.Devices, pynetbox.models.virtualization.VirtualMachines]


def setup_logging(verbose: bool = False) -> None:
    """Setup the logging with a custom format."""
    if not verbose:
        level = logging.INFO
        logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=level, stream=sys.stderr)
    else:
        level = logging.DEBUG
        logging.basicConfig(
            format='%(asctime)s [%(levelname)s] %(pathname)s:%(lineno)s %(message)s', level=level, stream=sys.stderr
        )

    logging.getLogger('requests').setLevel(logging.WARNING)  # Silence noisy logger


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Setup command line argument parser and return parsed args.

    Arguments:
        args (list, optional): an optional list of CLI arguments to parse.

    Returns:
        argparse.Namespace: The resulting parsed arguments.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-c', '--config', default='/etc/netbox/dns.cfg',
                        help='The config file to load. (default: %(default)s)')
    parser.add_argument('-v', '--verbose', help='Verbose mode.', action='store_true')
    subparsers = parser.add_subparsers(dest='command')

    commit = subparsers.add_parser('commit', help=('Generate and commit the DNS snippets to a local checkout, '
                                                   'interactively asking before pushing the changes to its remote.'))
    commit.add_argument('-b', '--batch', action='store_true',
                        help=('Enable the non-interactive mode, the commit will not be pushed to its remote and the '
                              'temporary directory will not be deleted. A JSON with the path of the temporary '
                              'directory and the SHA1 of the commit will be printed to the last line of stdout.'))
    commit.add_argument('--icinga-check', action='store_true',
                        help=('Enable the Icinga check mode. The commit will not be pushed to its remote, the '
                              'temporary directory will be deleted and the exit code will follow Icinga API. '
                              'Stderr should be redirect to /dev/null. Implies -b/--batch.'))
    commit.add_argument('--keep-files', action='store_true',
                        help='Do not delete the temporary repository. On no changes makes also an empty commit.')
    commit.add_argument('message', help='The commit message to use.')

    push = subparsers.add_parser('push', help=("Push a previously generated commit generated by the 'commit' command "
                                               "with the '-b/--batch' option set."))
    push.add_argument('path', type=Path,
                      help='The path of the temporary checkout where the commit to push was added.')
    push.add_argument('sha1', help='The SHA1 of the commit to push.')

    parsed_args = parser.parse_args(args)

    if parsed_args.command == 'commit' and parsed_args.icinga_check:
        parsed_args.batch = True

    if parsed_args.command == 'push':
        if not parsed_args.path.exists():
            parser.error("Path '{path}' does not exists.".format(path=parsed_args.path))
        if not parsed_args.path.stem.startswith(TMP_DIR_PREFIX):
            parser.error("Path '{path}' doesn't match the directory prefix '{prefix}'.".format(
                path=parsed_args.path, prefix=TMP_DIR_PREFIX))

    return parsed_args


class TimeoutHTTPAdapter(HTTPAdapter):

    def __init__(self, **kwargs: Any):
        """Initialize the adapter with a custom timeout.

        Params:
            As required by requests's HTTPAdapter:
            https://2.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter

        """
        self.timeout = 900
        super().__init__(**kwargs)

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:  # type: ignore
        """Override the send method to pass the adapter.

        Params:
            As required by requests's HTTPAdapter:
            https://2.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter.send
            The type ignore is needed unless the exact signature is replicated.

        """
        kwargs['timeout'] = self.timeout
        return super().send(request, **kwargs)


class Netbox:
    """Class to manage all data from Netbox."""

    NETBOX_DEVICE_STATUSES = ('active', 'planned', 'staged', 'failed', 'inventory', 'decommissioning',)
    NETBOX_DEVICE_MGMT_ONLY_STATUSES = ('inventory', 'decommissioning')

    def __init__(self, url: str, token: str):
        """Initialize the instance.

        Arguments:
            url (str): the Netbox API URL.
            token (str): the Netbox API token.

        """
        adapter = TimeoutHTTPAdapter()
        session = Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        self.api = pynetbox.api(url=url, token=token)
        self.api.http_session = session
        self.devices = defaultdict(lambda: {'addresses': set()})  # type: DefaultDict
        self.devices[NO_DEVICE_NAME]['device'] = None
        self.addresses = {}  # type: dict
        self.physical_interfaces = {}  # type: dict
        self.virtual_interfaces = {}  # type: dict
        self.prefixes = {}  # type: dict

    def changelog_since(self, since: datetime) -> bool:
        """Return True if there is any changelog in Netbox since the given datetime."""
        return bool(self.api.extras.object_changes.filter(time_after=since))

    def collect(self) -> None:
        """Collect all the data from Netbox. Must be called before using the class."""
        logger.info('Gathering devices, interfaces, addresses and prefixes from Netbox')
        self.addresses = {addr.id: addr for addr in self.api.ipam.ip_addresses.filter(status='active')}
        self.physical_interfaces = {interface.id: interface for interface in self.api.dcim.interfaces.all()}
        self.virtual_interfaces = {interface.id: interface for interface in self.api.virtualization.interfaces.all()}
        self.prefixes = {ipaddress.ip_network(prefix.prefix): prefix for prefix in self.api.ipam.prefixes.all()}

        for device in self.api.dcim.devices.filter(status=list(Netbox.NETBOX_DEVICE_STATUSES)):
            self._collect_device(device, True)

        for vm in self.api.virtualization.virtual_machines.all():
            self._collect_device(vm, False)

        for address in self.addresses.values():
            if address.interface is None:
                name = NO_DEVICE_NAME
                physical = False

                if not address.dns_name:
                    logger.debug('%s:%s has no DNS name', name, address)
                    continue
            else:
                try:
                    address.interface = self.physical_interfaces[address.interface.id]
                    name = address.interface.device.name
                    physical = True
                except KeyError:
                    address.interface = self.virtual_interfaces[address.interface.id]
                    name = address.interface.virtual_machine.name
                    physical = False

                if name not in self.devices:
                    logger.warning('Device %s of IP %s not in devices, skipping.', name, address)
                    continue

                if not address.dns_name:
                    logger.debug('%s:%s has no DNS name', name, address.interface.name)
                    continue

            self.devices[name]['addresses'].add(address)
            self.devices[name]['physical'] = physical

        logger.info('Gathered %d devices from Netbox', len(self.devices))

    def _collect_device(self, device: NetboxDeviceType, physical: bool) -> None:
        """Collect the given device (physical or virtual) based on its data."""
        self.devices[device.name]['device'] = device
        self.devices[device.name]['physical'] = physical
        for primary in (device.primary_ip4, device.primary_ip6):
            if primary is not None:
                if self.addresses[primary.id].dns_name:
                    self.devices[device.name]['addresses'].add(self.addresses[primary.id])
                else:
                    logger.debug('Primary address %s for device %s is missing a DNS name', primary, device.name)


class RecordBase:
    """Class to represent a base DNS record."""

    def __init__(self, zone: str, hostname: str, ip_interface: str):
        """Initialize the instance.

        Arguments:
            zone (str): the zone name.
            hostname (str): the hostname.
            ip_interface (str): the IP interface as returned by Netbox (e.g. 10.0.0.1/24).

        """
        self.zone = zone
        self.hostname = hostname
        self.interface = ipaddress.ip_interface(ip_interface)
        self.ip = self.interface.ip

    def __hash__(self) -> int:
        """Object hash.

        Params:
            According to Python's data model:
            https://docs.python.org/3/reference/datamodel.html#object.__hash__

        """
        return hash((self.zone, self.hostname, self.ip))

    def __eq__(self, other: object) -> bool:
        """Equality operator.

        Params:
            According to Python's data model:
            https://docs.python.org/3/reference/datamodel.html#object.__eq__

        """
        if not isinstance(other, RecordBase):
            return NotImplemented

        return self.zone == other.zone and self.hostname == other.hostname and self.ip == other.ip

    def __lt__(self, other: object) -> bool:
        """Less than operator.

        Params:
            According to Python's data model:
            https://docs.python.org/3/reference/datamodel.html?highlight=__lt__#object.__lt__

        """
        if not isinstance(other, RecordBase):
            return NotImplemented

        return self.to_tuple() < other.to_tuple()

    @abstractmethod
    def to_tuple(self) -> Tuple:
        """Tuple representation suitable to be used for sorting records.

        Returns:
            tuple: the tuple representation.

        """


class ReverseRecord(RecordBase):
    """Class to represent a reverse DNS record."""

    def __init__(self, zone: str, hostname: str, ip_interface: str, pointer: str):
        """Initialize the instance.

        Arguments:
            zone (str): the zone name.
            hostname (str): the hostname.
            ip_interface (str): the IP interface as returned by Netbox (e.g. 10.0.0.1/24).
            pointer (str): the part of the reverse pointer without the zone to use as key in the record.

        """
        super().__init__(zone, hostname, ip_interface)
        self.pointer = pointer

    def __str__(self) -> str:
        """String representation in DNS zonefile format of the record.

        Returns:
            str: the object representation.

        """
        return '{ptr} 1H IN PTR {hostname}.'.format(ptr=self.pointer.ljust(3), hostname=self.hostname)

    def to_tuple(self) -> Tuple:
        """Tuple representation suitable to be used for sorting records.

        Returns:
            tuple: the tuple representation.

        """
        return tuple([int(i, 16) for i in self.pointer.split('.')[::-1]] + [self.hostname])  # type: ignore


class ForwardRecord(RecordBase):
    """Class to represent a forward DNS record."""

    def __str__(self) -> str:
        """String representation in DNS zonefile format of the record.

        Returns:
            str: the object representation.

        """
        record_type = 'AAAA' if self.ip.version == 6 else 'A'
        # Fixed justification to avoid large diffs
        return '{hostname} 1H IN {type} {ip}'.format(
            hostname=self.hostname.ljust(40), type=record_type, ip=self.ip.compressed)

    def to_tuple(self) -> Tuple:
        """Tuple representation suitable to be used for sorting records.

        Returns:
            tuple: the tuple representation.

        """
        return ('.'.join(self.hostname.split('.')[::-1]), self.ip.exploded)

    def get_reverse(self, prefixes: KeysView) -> Optional[ReverseRecord]:
        """Return the reverse record of the current object.

        Arguments:
            prefixes (KeysView): the iterable of available prefixes from Netbox as ipaddress.ip_interface instances.

        Returns:
            ReverseRecord: the reverse object or None.

        """
        matching_prefixes = [prefix for prefix in prefixes if self.ip in prefix]
        if not matching_prefixes:  # External address not managed by us, skip the PTR entirely
            return None

        if self.ip.version == 6:  # For IPv6 PTRs we always split the zone at /64 and write the last 16 nibbles
            parts = self.ip.reverse_pointer.split('.')
            pointer = '.'.join(parts[:16])
            zone = '.'.join(parts[16:])
        else:
            # For IPv4 PTRs we always write the last octet and by default split at the /24 boundary.
            # For non-octet boundary sub-24 netmasks RFC 2317 suggestions are followed, using the hyphen '-'
            # as separator for the netmask instead of the slash '/'.
            pointer, zone = self.ip.reverse_pointer.split('.', 1)
            if self.interface.network.prefixlen > 24:
                sorted_prefixes_keys = sorted(matching_prefixes, key=attrgetter('prefixlen'), reverse=True)
                matched_prefix = sorted_prefixes_keys[0]
                if matched_prefix.prefixlen > 29:  # Consolidate into the parent prefix
                    matched_prefix = next(prefix for prefix in sorted_prefixes_keys if prefix.prefixlen <= 29)

                if matched_prefix.prefixlen > 24:
                    zone = matched_prefix.reverse_pointer.replace('/', '-')
                elif matched_prefix.prefixlen == 24:
                    zone = matched_prefix.network_address.reverse_pointer.split('.', 1)[1]
                else:  # No need to override, the pre-calculated zone is already correct
                    logger.debug('Parent container prefixlen for IP %s is smaller than /24 (/%d), forcing /24',
                                 self.ip, matched_prefix.prefixlen)

        return ReverseRecord(zone, '.'.join((self.hostname, self.zone)), str(self.interface), pointer)


class Records:
    """Class to represent all the DNS records."""

    def __init__(self, netbox: Netbox, min_records: int):
        """Initialize the instance.

        Arguments:
            netbox (Netbox): the Netbox instance.
            min_records (int): the minimum number of records that should be created, as a safety precaution.

        """
        self.netbox = netbox
        self.min_records = min_records
        self.zones = {'direct': defaultdict(set), 'reverse': defaultdict(set)}  # type: Dict

    def generate(self) -> None:
        """Generate all DNS record based on Netbox data."""
        logger.info('Generating DNS records')
        records_count = 0
        for name, device_data in self.netbox.devices.items():
            for address in device_data['addresses']:
                hostname, zone, zone_name = self._split_dns_name(address)
                records = Records._generate_address_records(
                    zone, hostname, address, device_data['device'], device_data['physical'])
                records_count += len(records)
                for record in records:
                    self.zones['direct'][zone_name].add(record)
                    reverse = record.get_reverse(self.netbox.prefixes.keys())
                    if reverse is not None:
                        self.zones['reverse'][reverse.zone].add(reverse)

        logger.info('Generated %d direct and reverse records (%d each) in %d direct zones and %d reverse zones',
                    records_count * 2, records_count, len(self.zones['direct']), len(self.zones['reverse']))

        if records_count < self.min_records:
            logger.error('CAUTION: the generated records are less than the minimum limit of %d. Check the diff!',
                         self.min_records)

    def write_snippets(self, destination: str) -> None:
        """Write the DNS zone snippet files into the destination directory.

        Arguments:
            destination (str): path where to save the snippet files.

        """
        logger.info('Generating zonefile snippets to directory %s', destination)
        for record_type, zones in self.zones.items():
            for zone, zone_records in zones.items():
                with open(os.path.join(destination, zone), 'w') as zonefile:
                    for record in sorted(zone_records):
                        zonefile.write(str(record) + '\n')

                logger.debug('Wrote %d %s records in %s zonefile', len(zone_records), record_type, zone)

    @staticmethod
    def _generate_address_records(zone: str, hostname: str, address: pynetbox.models.ipam.IpAddresses,
                                  device: Optional[NetboxDeviceType], physical: bool) -> List[ForwardRecord]:
        """Generate Record objects for the given address.

        Arguments:
            zone (str): the zone name.
            hostname (str): the hostname.
            address (pynetbox.models.ipam.IpAddresses): the Netbox address to use to generate the record.
            device (NetboxDeviceType): the Netbox device the address belongs to, either physical or virtual.
            physical (bool): if it's a physical device or not.

        Returns:
            list: a list of ForwardRecord objects related to the given address.

        """
        records = []
        if not physical or device is None or device.device_role.slug != 'server':
            records.append(ForwardRecord(zone, hostname, address.address))
        else:
            if device.status.value not in Netbox.NETBOX_DEVICE_MGMT_ONLY_STATUSES:
                # Primary/management with hostname IPs defined only in certain Netbox states
                records.append(ForwardRecord(zone, hostname, address.address))

            # Generate the additional asset tag mgmt record only for physical devices for which the hostname is
            # different from the asset tag.
            if (address.interface is not None and address.interface.mgmt_only and (
                    device.name.lower() != device.asset_tag.lower()
                    or device.status.value in Netbox.NETBOX_DEVICE_MGMT_ONLY_STATUSES)):
                records.append(ForwardRecord(zone, device.asset_tag.lower(), address.address))

        return records

    def _split_dns_name(self, address: pynetbox.models.ipam.IpAddresses) -> Tuple[str, str, str]:
        """Given a FQDN split it into hostname and zone.

        Arguments:
            address (pynetbox.models.ipam.IpAddresses): the address for which to split the FQDN.

        Returns:
            tuple: a 2-elements tuple with the hostname and the zone name.

        """
        parts = address.dns_name.strip().split('.')
        max_len = 2 + sum(1 for i in ('frack', 'mgmt', 'svc') if i in parts)

        split_len = min(len(parts) - 1, max_len)
        hostname = '.'.join(parts[:-split_len])
        zone = '.'.join(parts[-split_len:])
        zone_name = zone
        if not zone.endswith('.wmnet'):  # Split by datacenter based on the prefix for public zones or the device
            matching_prefixes = [prefix for prefix in self.netbox.prefixes
                                 if ipaddress.ip_interface(address.address).ip in prefix]
            if matching_prefixes:
                prefix_key = min(matching_prefixes, key=attrgetter('prefixlen'))
                if self.netbox.prefixes[prefix_key].site:
                    suffix = self.netbox.prefixes[prefix_key].site.slug
                else:
                    logger.debug('Failed to find DC for address %s, prefix %s, using global', address, prefix_key)
                    suffix = 'global'

            else:  # try to gather it from the device it's attached to
                if address.interface:
                    suffix = address.interface.device.site.slug
                else:
                    logger.warning('Failed to find DC for address %s from prefix or device, using global', address)
                    suffix = 'global'

            zone_name += '-' + suffix

        return hostname, zone, zone_name


def setup_repo(config: ConfigParser, tmpdir: str) -> git.Repo:
    """Setup the git repository working clone."""
    repo_path = config.get('dns_snippets', 'repo_path')
    logger.info('Cloning %s to %s ...', repo_path, tmpdir)
    origin_repo = git.Repo(repo_path)
    working_repo = origin_repo.clone(tmpdir)

    return working_repo


def get_file_stats(tmpdir: str) -> Tuple[int, int]:
    """Get file stats of the checkout."""
    lines = 0
    files = 0
    path = Path(tmpdir)
    for zone in path.glob('[!.]*'):
        files += 1
        with open(zone) as f:
            lines += sum(1 for line in f)

    logger.debug('Found %d existing files with %d lines', files, lines)
    return files, lines


def commit_changes(args: argparse.Namespace, working_repo: git.Repo) -> Optional[git.objects.commit.Commit]:
    """Add local changes and commit them, if any."""
    working_repo.index.add('*')
    if working_repo.head.is_valid() and not working_repo.index.diff(working_repo.head.commit):
        if args.keep_files:
            logger.info('Nothing to commit but --keep-files set, making an empty commit to allow local modifications')
        else:
            logger.info('Nothing to commit!')
            return None

    author = git.Actor(GIT_USER_NAME, GIT_USER_EMAIL)
    commit = working_repo.index.commit(args.message, author=author, committer=author)
    logger.info('Committed changes: %s', commit.hexsha)

    return commit


def validate_delta(changed: int, existing: int, warning: int, error: int, what: str) -> None:
    """Validate the percentage of changes and alert the user if over a threshold."""
    if existing == 0:
        return

    delta = changed * 100 / existing
    if delta > error:
        logging.error('CAUTION: %.1f%% of %s modified is over the error thresold. Check the diff!', delta, what)
    elif delta > warning:
        logging.warning('%.1f%% of %s modified is over the warning thresold', delta, what)
    else:
        logger.debug('%.1f%% of %s modified', delta, what)


def validate(files: int, lines: int, delta: Mapping[str, int]) -> None:
    """Validate the generated data."""
    logging.info('Validating generated data')
    validate_delta(delta['files'], files, WARNING_PERCENTAGE_FILES_CHANGED, ERROR_PERCENTAGE_FILES_CHANGED, 'files')
    validate_delta(delta['lines'], lines, WARNING_PERCENTAGE_LINES_CHANGED, ERROR_PERCENTAGE_LINES_CHANGED, 'lines')
    logging.info('Commit details: %s', delta)


def push(working_repo: git.Repo, sha1: str) -> int:
    """Push the committed changes to the repository's remote.

    Arguments:
        working_repo (git.Repo): the repository with the commit to push.
        sha1 (str): the sha1 of the commit to push.

    Returns:
        int: the return code of the operation.

    """
    head = working_repo.head.commit.hexsha
    if head != sha1:
        logger.error('Head SHA1 %s does not match given SHA1 %s', head, sha1)
        return INVALID_SHA1_RETURN_CODE

    push_info = working_repo.remote().push()[0]
    if push_info.flags & push_info.ERROR == push_info.ERROR:
        prefix = 'Error pushing, got'
        level = logging.ERROR
        ret_code = PUSH_ERROR_RETURN_CODE
    else:
        prefix = 'Pushed with'
        level = logging.INFO
        ret_code = 0

    logger.log(level, '%s bitflags %d: %s', prefix, push_info.flags, push_info.summary.strip())

    return ret_code


def run_commit(args: argparse.Namespace, config: ConfigParser, tmpdir: str) -> Tuple[Optional[Dict], int]:
    """Generate and commit the DNS snippets."""
    batch_status = None  # type: Optional[Dict[str, Any]]
    netbox = Netbox(config.get('netbox', 'api'), config.get('netbox', 'token_ro'))
    netbox.collect()
    records = Records(netbox, config.getint('dns_snippets', 'min_records'))
    records.generate()
    working_repo = setup_repo(config, tmpdir)
    files, lines = get_file_stats(tmpdir)
    working_repo.git.rm('.', r=True, ignore_unmatch=True)  # Delete all existing files to ensure removal of stale files
    records.write_snippets(tmpdir)
    commit = commit_changes(args, working_repo)

    if commit is None:
        if args.batch:
            batch_status = {'no_changes': True}
        return batch_status, NO_CHANGES_RETURN_CODE

    if not args.icinga_check:
        print(working_repo.git.show(['--color=always', 'HEAD']))
        validate(files, lines, commit.stats.total)

    if args.batch:
        batch_status = {'path': tmpdir, 'sha1': commit.hexsha}
        batch_status.update(commit.stats.total)
        return batch_status, 0

    answer = input('OK to push the changes to the {origin} repository? (y/n) '.format(
        origin=config.get('dns_snippets', 'repo_path')))
    if answer == 'y':
        ret_code = push(working_repo, commit.hexsha)
    else:
        logger.error('Manually aborted.')
        ret_code = ABORT_RETURN_CODE

    return batch_status, ret_code


def save_icinga_state(ret_code: int, netbox: Netbox, state_file: str) -> None:
    """Save a JSON status file so that can be consumed by an NRPE check."""
    if ret_code == NO_CHANGES_RETURN_CODE:
        message = 'Netbox has zero uncommitted DNS changes'
        ret_code = 0
    elif ret_code != 0:
        message = 'An error occurred checking if Netbox has uncommitted DNS changes'
        ret_code = 2
    else:
        if netbox.changelog_since(datetime.now() - timedelta(minutes=ALLOWED_CHANGES_MINUTES)):
            message = 'Netbox has uncommitted DNS changes, but last edit in Netbox is within {n} minutes'.format(
                n=ALLOWED_CHANGES_MINUTES)
            ret_code = 1
        else:
            message = 'Netbox has uncommitted DNS changes'
            ret_code = 2

    with open(state_file, 'w') as f:
        json.dump({'exit_code': ret_code, 'message': message, 'timestamp': time.time()}, f)


def check_icinga_should_run(state_file: str) -> bool:
    """Return True if the script should continue to update the state file, False if the state file is fresh enough."""
    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception as e:
        logger.error('Failed to read Icinga state from %s: %s', state_file, e)
        return True

    delta = time.time() - state['timestamp']
    logger.info('Last run was %d seconds ago with exit code %d', delta, state['exit_code'])
    if state['exit_code'] == 0:
        if delta > ICINGA_RUN_EVERY_MINUTES * 60:
            return True

        logger.info('Skipping')
        return False

    if delta > ICINGA_RETRY_ON_FAILURE_MINUTES * 60:
        return True

    logger.info('Skipping')
    return False


def main() -> int:
    """Execute the script."""
    args = parse_args()
    setup_logging(args.verbose)
    config = ConfigParser()
    config.read(args.config)
    icinga_state_file = config.get('icinga', 'state_file')

    if args.command == 'commit' and args.icinga_check and not check_icinga_should_run(icinga_state_file):
        return 0

    batch_status = None
    ret_code = EXCEPTION_RETURN_CODE
    try:
        if args.command == 'commit':
            tmpdir = tempfile.mkdtemp(prefix=TMP_DIR_PREFIX)
            batch_status, ret_code = run_commit(args, config, tmpdir)
        elif args.command == 'push':
            tmpdir = str(args.path)
            ret_code = push(git.Repo(tmpdir), args.sha1)

    except Exception:
        logger.exception('Failed to run')
        ret_code = EXCEPTION_RETURN_CODE

    finally:
        if args.command == 'commit' and not args.batch and ret_code not in (0, NO_CHANGES_RETURN_CODE):
            print('An error occurred, the generated files can be inspected in {tmpdir}'.format(tmpdir=tmpdir))
            input('Press any key to cleanup the generated files and exit ')

        if ((args.command == 'commit' and not args.batch and not args.keep_files)
                or (args.command == 'commit' and ret_code == NO_CHANGES_RETURN_CODE and not args.keep_files)
                or (args.command == 'commit' and args.icinga_check)
                or (args.command == 'push' and ret_code == 0)):
            shutil.rmtree(tmpdir, ignore_errors=True)
            logger.info('Temporary directory %s removed.', tmpdir)

        if batch_status is not None and not args.icinga_check:
            print('METADATA:', json.dumps(batch_status))

    if args.command == 'commit' and args.icinga_check:
        save_icinga_state(ret_code, Netbox(config.get('netbox', 'api'), config.get('netbox', 'token_ro')),
                          icinga_state_file)
        if ret_code == NO_CHANGES_RETURN_CODE:
            ret_code = 0

    return ret_code


if __name__ == '__main__':
    sys.exit(main())
