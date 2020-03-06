#!/usr/bin/env python3
"""Generate DNS zonefile snippets with records from Netbox to be included in zonefiles.

Todo:
    * Support a two-phase push to integrate with a cookbook.
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

from abc import abstractmethod
from collections import defaultdict
from configparser import ConfigParser
from operator import attrgetter
from pathlib import Path
from typing import DefaultDict, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import git
import pynetbox

from git.util import get_user_id


logger = logging.getLogger()
GIT_USER_NAME = 'generate-dns-snippets'
GIT_USER_EMAIL = 'noc@wikimedia.org'
NO_CHANGES_RETURN_CODE = 99
WARNING_PERCENTAGE_LINES_CHANGED = 3
ERROR_PERCENTAGE_LINES_CHANGED = 5
WARNING_PERCENTAGE_FILES_CHANGED = 8
ERROR_PERCENTAGE_FILES_CHANGED = 15


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
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-c', '--config', help='The config file to load.', default='/etc/netbox/dns.cfg')
    parser.add_argument('-v', '--verbose', help='Verbose mode.', action='store_true')
    parser.add_argument('-b', '--batch', action='store_true',
                        help=('Enable the non-interactive mode, the commit will not be pushed to its remote and the '
                              'temporary directory will not be deleted. A JSON with the path of the temporary '
                              'directory and the SHA1 of the commit will be printed to the last line of stdout.'))
    parser.add_argument('message', help='The commit message to use.')

    return parser.parse_args(args)


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
        self.api = pynetbox.api(url=url, token=token)
        self.devices = defaultdict(lambda: {'addresses': set()})  # type: DefaultDict
        self.addresses = {}  # type: dict
        self.interfaces = {}  # type: dict
        self.prefixes = set()  # type: Set

    def collect(self) -> None:
        """Collect all the data from Netbox. Must be called before using the class."""
        logger.info('Gathering devices, interfaces, addresses and prefixes from Netbox')
        self.addresses = {addr.id: addr for addr in self.api.ipam.ip_addresses.all()}
        self.interfaces = {interface.id: interface for interface in self.api.dcim.interfaces.all()}
        self.prefixes = {ipaddress.ip_network(prefix.prefix) for prefix in self.api.ipam.prefixes.all()}

        for device in self.api.dcim.devices.filter(status=list(Netbox.NETBOX_DEVICE_STATUSES)):
            self.devices[device.name]['device'] = device
            if device.primary_ip4 is not None:
                self.devices[device.name]['addresses'].add(self.addresses[device.primary_ip4.id])
            if device.primary_ip6 is not None:
                self.devices[device.name]['addresses'].add(self.addresses[device.primary_ip6.id])

        for address in self.addresses.values():
            address.interface = self.interfaces[address.interface.id]
            if address.interface.device.name not in self.devices:
                logger.warning('Device %s of IP %s not in devices, skipping.', address.interface.device.name, address)
                continue

            if not address.dns_name:
                logger.warning('%s:%s has no DNS name', address.interface.device.name, address.interface.name)
                continue

            self.devices[address.interface.device.name]['addresses'].add(address)

        logger.info('Gathered %d devices from Netbox', len(self.devices))


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

    def __eq__(self, other: object) -> bool:
        """Equality operator.

        Params:
            According to Python's data model:
            https://docs.python.org/3/reference/datamodel.html?highlight=__lt__#object.__eq__

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
        """Tuple representatin suitable to be used for sorting records.

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
        """Tuple representatin suitable to be used for sorting records.

        Returns:
            tuple: the tuple representation.

        """
        return tuple([int(i) for i in self.pointer.split('.')] + [self.hostname])  # type: ignore


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
        """Tuple representatin suitable to be used for sorting records.

        Returns:
            tuple: the tuple representation.

        """
        return ('.'.join(self.hostname.split('.')[::-1]), self.ip.exploded)

    def get_reverse(self, prefixes: Set) -> ReverseRecord:
        """Return the reverse record of the current object.

        Arguments:
            prefixes (set): the set of available prefixes from Netbox.

        Returns:
            ReverseRecord: the reverse record object.

        """
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
                max_prefix = max([prefix for prefix in prefixes if self.ip in prefix], key=attrgetter('prefixlen'))
                if max_prefix.prefixlen > 24:
                    zone = max_prefix.reverse_pointer.replace('/', '-')
                else:
                    zone = max_prefix.network_address.reverse_pointer

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
        self.zones = {'direct': defaultdict(list), 'reverse': defaultdict(list)}  # type: Dict

    def generate(self) -> None:
        """Generate all DNS record based on Netbox data."""
        logger.info('Generating DNS records')
        records_count = 0
        for name, device_data in self.netbox.devices.items():
            for address in device_data['addresses']:
                hostname, zone = Records._split_dns_name(address.dns_name)
                records = Records._generate_address_records(zone, hostname, address, device_data['device'])
                records_count += len(records)
                for record in records:
                    self.zones['direct'][zone].append(record)
                    reverse = record.get_reverse(self.netbox.prefixes)
                    self.zones['reverse'][reverse.zone].append(reverse)

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
                                  device: pynetbox.models.dcim.Devices) -> List[ForwardRecord]:
        """Generate Record objects for the given address.

        Arguments:
            zone (str): the zone name.
            address (pynetbox.models.ipam.IpAddresses): the Netbox address to use to generate the record.
            device (pynetbox.models.dcim.Devices): the Netbox device the address belongs to.

        Returns:
            list: a list of ForwardRecord objects related to the given address.

        """
        records = []
        if device.status.value not in Netbox.NETBOX_DEVICE_MGMT_ONLY_STATUSES or device.device_role.slug != 'server':
            # Some states must have only the mgmt record for the asset tag
            records.append(ForwardRecord(zone, hostname, address.address))

        # Generate the additional asset tag mgmt record only if the Netbox name is not the asset tag already
        if (address.interface.mgmt_only and device.device_role.slug == 'server'
                and (device.name.lower() != device.asset_tag.lower()
                     or device.status.value in Netbox.NETBOX_DEVICE_MGMT_ONLY_STATUSES)):
            records.append(ForwardRecord(zone, device.asset_tag.lower(), address.address))

        return records

    @staticmethod
    def _split_dns_name(dns_name: str) -> Tuple[str, str]:
        """Given a FQDN split it into hostname and zone.

        Arguments:
            dns_name (str): the FQDN to split.

        Returns:
            tuple: a 2-elements tuple with the hostname and the zone name.

        """
        parts = dns_name.strip().split('.')
        max_len = 2
        if 'frack' in parts:
            max_len += 1
        if 'mgmt' in parts:
            max_len += 1

        split_len = min(len(parts) - 1, max_len)
        hostname = '.'.join(parts[:-split_len])
        zone = '.'.join(parts[-split_len:])

        return hostname, zone


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
        logger.info('Nothing to commit!')
        return None

    author = git.Actor(GIT_USER_NAME, GIT_USER_EMAIL)
    message = '{user}: {message}'.format(user=get_user_id(), message=args.message)
    commit = working_repo.index.commit(message, author=author, committer=author)
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


def run(args: argparse.Namespace, config: ConfigParser, tmpdir: str) -> int:
    """Generate and commit the DNS snippets."""
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
            print(json.dumps({'no_changes': True}))
        return NO_CHANGES_RETURN_CODE

    print(working_repo.git.show(['--color=always', 'HEAD']))
    validate(files, lines, commit.stats.total)

    if args.batch:
        print(json.dumps({'path': tmpdir, 'sha1': commit.hexsha}))
        return 0

    answer = input('OK to push the changes to the {origin} repository? (y/n) '.format(
        origin=config.get('dns_snippets', 'repo_path')))
    if answer == 'y':
        push_info = working_repo.remote().push()[0]
        if push_info.flags & push_info.ERROR == push_info.ERROR:
            level = logging.ERROR
            exit_code = 2
        else:
            level = logging.INFO
            exit_code = 0

        logger.log(level, 'Pushed with bitflags %d: %s %s',
                   push_info.flags, push_info.summary.strip(), commit.stats.total)

    else:
        logger.error('Manually aborted.')
        exit_code = 3

    return exit_code


def main() -> int:
    """Execute the script."""
    args = parse_args()
    setup_logging(args.verbose)
    config = ConfigParser()
    config.read(args.config)

    tmpdir = tempfile.mkdtemp(prefix='dns-snippets-')
    exit_code = 1
    try:
        exit_code = run(args, config, tmpdir)

    except Exception:
        logger.exception('Failed to run')
        exit_code = 4

    finally:
        if exit_code not in (0, NO_CHANGES_RETURN_CODE):
            print('An error occurred, the generated files can be inspected in {tmpdir}'.format(tmpdir=tmpdir))
            input('Press any key to cleanup the generated files and exit ')

        if not args.batch or exit_code == NO_CHANGES_RETURN_CODE:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
