import csv
import io

from string import ascii_lowercase

from django.core.exceptions import ObjectDoesNotExist

from dcim.choices import InterfaceTypeChoices
from dcim.models import Device, Interface
from extras.scripts import BooleanVar, ChoiceVar, FileVar, IntegerVar, ObjectVar, Script, StringVar
from ipam.models import IPAddress, Prefix, VLAN

from _common import find_tor, format_logs, port_to_iface, duplicate_cable_id, Importer

MGMT_IFACE_NAME = "mgmt"
PRIMARY_IFACE_NAME = "##PRIMARY##"
VLAN_TYPES = (
    "",  # Default value
    "public",
    "private",
    "analytics",
    "cloud-hosts",
)
VLAN_POP_TYPES = ("public", "private")
FRACK_TENANT_SLUG = "fr-tech"

CSV_HEADERS = ('device',
               'z_nbdevice',
               'vlan',
               'vlan_type',
               'skip_ipv6_dns',
               'cassandra_instances',
               'z_port',
               'interface_type',
               'cable_id')


class ProvisionServerNetworkCSV(Script):

    class Meta:
        name = "Provision multiple servers network attributes from a CSV"
        description = "More exactly: IPs, interfaces (including mgmt and switch), primary cable, vlan."
        commit_default = False

    csv_file = FileVar(
        required=True,
        label="CSV import",
        description="Template and example on https://phabricator.wikimedia.org/F32411089"
    )

    def run(self, data, commit):
        reader = csv.DictReader(io.StringIO(data['csv_file'].read().decode('utf-8')))

        for row in reader:
            try:
                data = self._transform_csv(row)
            except csv.Error as e:
                self.log_failure(f"Error parsing row {reader.line_num}: {e}")
                continue
            if not data:
                # If any issue with the transform (eg. typoed host), ignore the row
                continue
            provision_script = ProvisionServerNetwork()
            provision_script.provision_server(data)
            self.log.extend(provision_script.log)
        return format_logs(self.log)

    def _transform_csv(self, row):
        "Transform the CSV fields to Netbox objects."

        for header in CSV_HEADERS:
            try:
                row[header]
            except KeyError:
                self.log_failure(f"CSV header {header} missing, skipping.")
                return
        # Ensure that no cells are missing, not empty cells, but missing cells
        if any(value is None for value in row.values()):
            self.log_failure(f"{row['device']}: missing CSV cells, skipping.")
            return
        try:
            row['device'] = Device.objects.get(name=row['device'])
        except ObjectDoesNotExist:
            self.log_failure(f"{row['device']}: device not found, skipping.")
            return
        try:
            row['z_nbdevice'] = Device.objects.get(name=row['z_nbdevice'])
        except ObjectDoesNotExist:
            self.log_failure(f"{row['device']}: switch {row['z_nbdevice']} not found, skipping.")
            return
        if row['vlan']:
            try:
                row['vlan'] = VLAN.objects.get(name=row['vlan'], site=row['device'].site)
            except ObjectDoesNotExist:
                self.log_failure(f"{row['device']}: vlan {row['vlan']} not found, skipping.")
                return
        row['skip_ipv6_dns'] = bool(int(row['skip_ipv6_dns']))
        row['z_port'] = int(row['z_port'])
        if row['cassandra_instances']:
            row['cassandra_instances'] = int(row['cassandra_instances'])
        else:
            row['cassandra_instances'] = 0

        return row


class ProvisionServerNetwork(Script, Importer):

    class Meta:
        name = "Provision a server's network attributes"
        description = "More exactly: IPs, interfaces (including mgmt and switch), primary cable, vlan."
        commit_default = False

    device = ObjectVar(
        required=True,
        description=("Inventory or planned server. (Required)"),
        model=Device,
        query_params={
            'role': 'server',
            'status': ('inventory', 'planned'),
        }
    )

    z_port = IntegerVar(label="Switch port",
                        description="Physical port number (0-48) (Required)",
                        required=True,
                        min_value=0,
                        max_value=48)

    interface_type_choices = (
        (InterfaceTypeChoices.TYPE_1GE_FIXED, '1G'),
        (InterfaceTypeChoices.TYPE_10GE_SFP_PLUS, '10G'),
        (InterfaceTypeChoices.TYPE_25GE_SFP28, '25G')
    )
    interface_type = ChoiceVar(label="Interface type/speed",
                               description="Interface speed (Required)",
                               required=True,
                               choices=interface_type_choices)

    cable_id = StringVar(label="Cable ID", required=False)

    skip_ipv6_dns = BooleanVar(
        required=False,
        label="Skip IPv6 DNS records.",
        description=("Skip the generation of the IPv6 DNS records. Enable if the devices don't yet fully support "
                     "IPv6."),
    )

    z_nbdevice = ObjectVar(
        label="Switch",
        required=False,
        description=("Top of rack switch, optional, if not set will try to find it automatically."),
        model=Device,
        query_params={'role': ('asw', 'cloudsw'), }
    )

    cassandra_instances = ChoiceVar(
        required=False,
        choices=[(i, i) for i in range(6)],
        label="How many Cassandra instances",
        description=("To be set only for hosts that will run Cassandra. This many additional IPv4s will be "
                     "allocated and their DNS name will be set to $HOSTNAME-a, $HOSTNAME-b, etc."),
    )
    vlan_type = ChoiceVar(
        required=False,
        choices=[(value, value if value else "-" * 9) for value in VLAN_TYPES],
        label="VLAN Type",
        description=("The VLAN type to use for assigning the primary IPs. The specific VLAN will be automatically "
                     "chosen based on the device. For not yet supported cases use the VLAN parameter below. The "
                     "VLAN Type and VLAN parameters are mutually exclusive."),
    )
    vlan = ObjectVar(
        required=False,
        label="VLAN",
        description=("Select the specific VLAN if the VLAN Type parameter doesn't support the device's VLAN. The "
                     "VLAN Type and VLAN parameters are mutually exclusive."),
        model=VLAN,
        query_params={
            "group": "production",
            "status": "active",
            "name__nisw": [f"{vlan}{i}-" for vlan in VLAN_TYPES if vlan for i in (1, 2)],
        }
    )

    def run(self, data, commit):
        """Run the script and return all the log messages."""
        self.log_info(f"Called with parameters: {data}")
        self.provision_server(data)
        return format_logs(self.log)

    def provision_server(self, data):
        """Process a single device."""
        device = data['device']
        z_nbdevice = data['z_nbdevice']
        z_port = data['z_port']
        interface_type = data['interface_type']
        cable_id = data['cable_id']
        assign_mgmt = True

        if not data["vlan_type"] and not data["vlan"]:
            self.log_failure(f"{device}: one parameter between VLAN Type and VLAN must be specified, skipping.")
            return

        if data["vlan_type"] and data["vlan"]:
            self.log_failure(f"{device}: only one parameter between VLAN Type and VLAN can be specified, skipping.")
            return

        if device.status not in ("inventory", "planned"):
            self.log_failure(
                f"{device}: status {device.status}, expected Inventory or Planned, skipping."
            )
            return

        if not device.rack:
            self.log_failure(f"{device}: missing rack information, skipping.")
            return

        if device.device_role.slug != "server":
            self.log_failure(
                f"{device.name}: role {device.device_role}, only servers are supported, skipping."
            )
            return

        if cable_id and duplicate_cable_id(cable_id, device.site):
            self.log_failure(f"Cable ID {cable_id} already assigned in {device.site.slug}.")
            return

        if not z_nbdevice:
            z_nbdevice = find_tor(device)
            if not z_nbdevice:
                self.log_failure(f"{device}: Can't find an adequate top-or-rack switch in rack {device.rack}. "
                                 "please double check or select it manually.")
                return
        else:
            if z_nbdevice.rack != device.rack:
                self.log_failure(f"{device}: switch {z_nbdevice} not in same rack as server.")
                return

        ifaces = device.interfaces.all()
        #  If the device have interface(s)
        if ifaces:
            # But it's only the mgmt, continue with creating the primary
            if (len(ifaces) == 1 and ifaces[0].name == MGMT_IFACE_NAME and ifaces[0].count_ipaddresses == 1
                    and ifaces[0].ip_addresses.all()[0].dns_name):
                self.log_warning(f"{device}: Skipping assignment of MGMT interface because already allocated")
                assign_mgmt = False
            else:
                # All the interfaces exist (mgmt & revenue), don't go further
                ifaces_list = ", ".join(i.name for i in ifaces)
                self.log_failure(f"{device}: interfaces already defined: {ifaces_list}, skipping.")
                return

        # Assigning first the primary IPs as it can fail some validation step
        if data["vlan_type"]:
            vlan = self._get_vlan(data["vlan_type"], device)
            if vlan is None:
                return
        else:
            vlan = data["vlan"]

        if not self._is_vlan_valid(vlan, device):
            return

        if device.tenant is not None and device.tenant.slug == FRACK_TENANT_SLUG:
            self.log_warning(f"{device}: Skipping Primary IP allocation with tenant {device.tenant}. "
                             "Primary IP allocation for Fundraising Tech is done manually in the DNS repository.")
        else:
            try:
                cassandra_instances = int(data["cassandra_instances"])
            except ValueError:
                # if this is not set it defaults to ''
                cassandra_instances = 0
            nbiface = self._assign_primary(device, vlan, iface_type=interface_type, skip_ipv6_dns=data["skip_ipv6_dns"],
                                           cassandra_instances=cassandra_instances)

            # Now that we're done with the primary interface, we tackle the switch side
            z_iface = port_to_iface(z_port, z_nbdevice, interface_type)
            if not z_iface:
                self.log_failure(f"{z_nbdevice}: invalid port/type/device combination.")
                return
            z_nbiface = self._update_z_nbiface(z_nbdevice, z_iface, vlan)

            # And now the cable between the two
            # If the switch port already have a cable, we don't try to delete it
            if z_nbiface.cable:
                self.log_warning(f"There is already a cable on {z_nbiface.device}:{z_nbiface} (typo?), "
                                 f"Skipping cable creation, please do it manually")
                return
            self._create_cable(nbiface, z_nbiface, label=cable_id if cable_id else '')

        if assign_mgmt:
            self._assign_mgmt(device)

    def _assign_mgmt(self, device):
        """Create a management interface in the device and assign to it a management IP."""
        iface_type = InterfaceTypeChoices.TYPE_1GE_FIXED
        iface = self._add_iface(MGMT_IFACE_NAME, device, iface_type=iface_type, mgmt=True)

        # determine prefix appropriate to site of device
        try:
            prefix = Prefix.objects.get(
                site=device.site, role__slug="management", tenant=device.tenant, status="active"
            )
        except ObjectDoesNotExist:
            self.log_failure(f"{device}: can't find management prefix for site {device.site.slug}.")
            return

        self.log_debug(f"{device}: selecting address from prefix {prefix.prefix}")
        ip_address = prefix.get_first_available_ip()
        if ip_address is None:
            self.log_failure(f"{device}: unable to find an available IP in prefix {prefix.prefix}")
            return

        if device.tenant and device.tenant.slug == FRACK_TENANT_SLUG:
            dns_name = f"{device.name}.mgmt.frack.{device.site.slug}.wmnet"
        else:
            dns_name = f"{device.name}.mgmt.{device.site.slug}.wmnet"

        self._add_ip(ip_address, dns_name, prefix, iface, device)

    def _assign_primary(self, device, vlan, *, iface_type, skip_ipv6_dns=False, cassandra_instances=0):
        """Create a primary interface in the device and assign to it an IPv4, a mapped IPv6 and related DNS records.

        If Cassandra instances is greater than zero allocate additional IPs for those with hostname
        $HOSTNAME-a, $HOSTNAME-b, etc.

        """
        # We create the interface so IP assignment doesn't impact the physical layer
        iface = self._add_iface(PRIMARY_IFACE_NAME, device, iface_type=iface_type, mgmt=False)

        prefixes_v4 = vlan.prefixes.filter(prefix__family=4, status="active")  # Must always be one
        prefixes_v6 = vlan.prefixes.filter(prefix__family=6, status="active")  # Can either be one or not exists
        if len(prefixes_v4) != 1 or len(prefixes_v6) > 1:
            self.log_warning(f"{device}: unsupported case, found {len(prefixes_v4)} v4 prefixes and "
                             f"{len(prefixes_v6)} v6 prefixes, expected 1 and 0 or 1 respectively, "
                             "skipping IP allocation.")
            return iface

        prefix_v4 = prefixes_v4[0]
        prefix_v6 = None
        if prefixes_v6:
            prefix_v6 = prefixes_v6[0]

        self.log_debug(f"{device}: selecting address from prefix {prefix_v4.prefix}")

        ip_address = prefix_v4.get_first_available_ip()
        if ip_address is None:
            self.log_warning(f"{device}: unable to find an available IP in prefix {prefix_v4.prefix}, "
                             "skipping IP allocation.")
            return iface

        if prefix_v4.prefix.is_private():
            if device.tenant and device.tenant.slug == FRACK_TENANT_SLUG:
                dns_suffix = f"frack.{device.site.slug}.wmnet"
            else:
                dns_suffix = f"{device.site.slug}.wmnet"
        else:
            dns_suffix = "wikimedia.org"

        dns_name = f"{device.name}.{dns_suffix}"
        ip_v4 = self._add_ip(ip_address, dns_name, prefix_v4, iface, device)
        device.primary_ip4 = ip_v4
        device.save()
        self.log_success(f"{device}: marked IPv4 address {ip_v4} as primary IPv4 for device.")

        # Allocate additional IPs
        for letter in ascii_lowercase[:cassandra_instances]:
            extra_ip_address = prefix_v4.get_first_available_ip()
            extra_dns_name = f"{device.name}-{letter}.{dns_suffix}"
            self._add_ip(extra_ip_address, extra_dns_name, prefix_v4, iface, device)

        if prefix_v6 is None:
            self.log_warning(f"{device}: no IPv6 prefix found for VLAN {vlan.name}, skipping IPv6 allocation.")
            # Whatever happen, as long as the interface is created, return it
            return iface

        dns_name_v6 = dns_name
        if skip_ipv6_dns:
            self.log_info(f"{device}: Not assigning DNS name to the IPv6 address as requested.")
            dns_name_v6 = ""

        # Generate the IPv6 address embedding the IPv4 address, for example from an IPv4 address 10.0.0.1 and an
        # IPv6 prefix 2001:db8:3c4d:15::/64 the mapped IPv6 address 2001:db8:3c4d:15:10:0:0:1/64 is generated.
        prefix_v6_base, prefix_v6_mask = str(prefix_v6).split("/")
        mapped_v4 = str(ip_v4).split('/')[0].replace(".", ":")
        ipv6_address = f"{prefix_v6_base.rstrip(':')}:{mapped_v4}/{prefix_v6_mask}"
        ip_v6 = self._add_ip(ipv6_address, dns_name_v6, prefix_v6, iface, device)
        device.primary_ip6 = ip_v6
        device.save()
        self.log_success(f"{device}: marked IPv6 address {ip_v6} as primary IPv6.")

        # Whatever happen, as long as the interface is created, return it
        return iface

    def _get_vlan(self, vlan_type, device):
        """Find and return the appropriate VLAN that matches the type and device location."""
        new_vlan_name = f"{vlan_type}1-{device.rack.name.lower()}-{device.site.slug}"

        if device.site.slug in ("eqiad", "codfw"):
            # TODO: add support for additional VLANs of a given type (e.g. private2)
            if vlan_type == 'cloud-hosts':
                old_vlan_name = f"cloud-hosts1-{device.site.slug}"
            else:
                old_vlan_name = f"{vlan_type}1-{device.rack.location.slug.split('-')[-1]}-{device.site.slug}"
        else:
            if vlan_type not in VLAN_POP_TYPES:
                self.log_failure(f"{device}: VLAN type {vlan_type} not available in site {device.site.slug}, skipping.")
                return

            old_vlan_name = f"{vlan_type}1-{device.site.slug}"

        try:
            return VLAN.objects.get(name=new_vlan_name, status="active")
        except ObjectDoesNotExist:
            try:
                return VLAN.objects.get(name=old_vlan_name, status="active")
            except ObjectDoesNotExist:
                self.log_failure(
                    f"{device}: unable to find VLAN with name {old_vlan_name} or {new_vlan_name}, skipping.")

    def _is_vlan_valid(self, vlan, device):
        """Try to ensure that the VLAN matches the device location."""
        if vlan.site != device.site:
            self.log_failure(
                f"{device}: mismatch site for VLAN {vlan.name}: "
                f"{device.site.slug} (device) != {vlan.site.slug} (VLAN), skipping."
            )
            return False

        if vlan.tenant != device.tenant:
            self.log_failure(
                f"{device}: , mismatch tenant for VLAN {vlan.name}: "
                f"{device.tenant} (device) != {vlan.tenant} (VLAN), skipping."
            )
            return False

        # Attempt to validate the row for old vlan names, for the new names the vlans are per-row
        devices = {i.device for i in vlan.get_interfaces()}
        if not devices:  # This is the first device of a new VLAN, can't validate it
            return True

        racks = {dev.rack for dev in devices}
        rack_locations = {rack.location for rack in racks}
        if device.rack.location not in rack_locations:
            self.log_failure(f"{device} is in row {device.rack.location} but VLAN {vlan.name} is present only in "
                             f"{rack_locations}. Skipping device because of invalid VLAN.")
            return False

        if device.rack not in racks:
            self.log_warning(f"{device} is the first device in rack {device.rack} to be added to VLAN {vlan.name}, "
                             f"unable to automatically verify if that's correct, please double check.")

        return True

    def _add_iface(self, name, device, *, iface_type, mgmt=False):
        """Add an interface to the device."""
        iface = Interface(name=name, mgmt_only=mgmt, device=device, type=iface_type)
        iface.save()
        self.log_success(f"{device}: created interface {name} (mgmt={mgmt})")
        return iface

    def _add_ip(self, address, dns_name, prefix, iface, device):
        """Assign an IP address to the interface."""
        address = IPAddress(
            address=address,
            status="active",
            dns_name=dns_name,
            vrf=prefix.vrf.pk if prefix.vrf else None,
            assigned_object=iface,
            tenant=device.tenant,
        )
        address.save()
        self.log_success(f"{device}: assigned IPv{prefix.family} {address} to interface {iface.name} "
                         f"with DNS name '{dns_name}'.")

        return address
