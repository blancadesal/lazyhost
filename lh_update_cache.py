import configparser
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

LOCAL_KNOWN_HOSTS = Path("~/.ssh/known_hosts").expanduser()
CONFIG = Path("~/.config/lazyhost/config.ini").expanduser()
config = configparser.ConfigParser()
config.read(CONFIG)
DEFAULT_CACHE_PATH = Path("~/.cache/lazyhost").expanduser()


def get_cache_file(name: str) -> Path:
    suffix = "json" if name == "fqdn" else "txt"
    return Path(DEFAULT_CACHE_PATH / f"{name}.{suffix}")


cachefiles = {
    key: get_cache_file(key)
    for key in [
        "local_known_hosts",
        "known_hosts",
        "openstackbrowser",
        "netbox_virtual",
        "netbox_physical",
        "fqdn",
        "merged",
    ]
}


def write_to_cache(data, cachefile):
    cachefile.write_text(data)


def merge_cachefiles():
    cachefiles_to_merge = [
        cachefiles[key]
        for key in [
            "local_known_hosts",
            "known_hosts",
            "openstackbrowser",
            "netbox_virtual",
            "netbox_physical",
        ]
    ]
    merged_data = []
    for cachefile in cachefiles_to_merge:
        if cachefile.exists():
            with open(cachefile, "r") as f:
                merged_data.extend(f.read().splitlines())
    merged_data = sorted(set(merged_data))
    cachefiles["merged"].write_text("\n".join(merged_data))


def fetch_all_results(url, **kwargs):
    results = []
    while url:
        response = requests.get(url, **kwargs)
        data = response.json()
        results.extend(data["results"])
        url = data["next"]
    return results


@dataclass
class NetboxHost:
    name: str
    slug: str
    cachefile: Optional[Path] = None
    primary_ip_url: Optional[str] = None
    dns_name: Optional[str] = None

    def get_fqdn(self, **kwargs):
        if not self.primary_ip_url:
            self.dns_name = f"{self.name}.{self.slug}.wmnet"
            return
        if self.cachefile:
            self._get_fqdn_from_cache()
        if not self.dns_name:
            self._get_fqdn_from_api(**kwargs)

    def _get_fqdn_from_cache(self):
        with open(self.cachefile, "r") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
            self.dns_name = data.get(self.primary_ip_url, None)

    def _write_fqdn_to_cache(self):
        with open(self.cachefile, "r+") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
            data[self.primary_ip_url] = self.dns_name
            f.seek(0)
            json.dump(data, f)
            f.truncate()

    def _get_fqdn_from_api(self, **kwargs):
        response = requests.get(url=self.primary_ip_url, **kwargs)
        response.raise_for_status()
        ip_info = response.json()
        dns_name = ip_info["dns_name"]
        if dns_name:
            self.dns_name = dns_name
            if self.cachefile:
                self._write_fqdn_to_cache()


def get_openstack_hosts(url):
    all_vms_response = requests.get(url)
    all_vms_response.raise_for_status()
    return all_vms_response.text


def get_known_hosts(url):
    all_known_hosts_response = requests.get(url)
    all_known_hosts_response.raise_for_status()
    clean_hosts = [
        host_line.split(",", 1)[0]
        for host_line in all_known_hosts_response.text.splitlines()
    ]
    return "\n".join(clean_hosts)


def get_local_known_hosts(filepath):
    with open(filepath, "r") as f:
        clean_hosts = list(set(line.split(None, 1)[0] for line in f))
    return "\n".join(clean_hosts)


def get_netbox_hosts(url, cachefile, **kwargs):
    hosts = []
    for result in fetch_all_results(url, **kwargs):
        device = NetboxHost(
            name=result["name"], slug=result["site"]["slug"], cachefile=cachefile
        )
        if result["primary_ip"]:
            device.primary_ip_url = result["primary_ip"]["url"]
        device.get_fqdn(**kwargs)
        if device.dns_name:
            hosts.append(device.dns_name)
    return "\n".join(hosts)


def update_openstack_cache():
    logging.info("Updating OpenStack cache")
    openstack_hosts = get_openstack_hosts(config["openstack-browser"]["url"])
    write_to_cache(openstack_hosts, cachefiles["openstackbrowser"])


def update_known_hosts_cache():
    logging.info("Updating known hosts cache")
    known_hosts = get_known_hosts(config["known-hosts"]["url"])
    write_to_cache(known_hosts, cachefiles["known_hosts"])


def update_local_known_hosts_cache():
    logging.info("Updating local known hosts cache")
    local_known_hosts = get_local_known_hosts(LOCAL_KNOWN_HOSTS)
    write_to_cache(local_known_hosts, cachefiles["local_known_hosts"])


def update_netbox_virtual_cache():
    logging.info("Updating Netbox virtual cache")
    netbox_virtual_url = f"{config['netbox']['url']}/virtualization/virtual-machines/"
    headers = {"Authorization": f"Token {config['netbox']['api_token']}"}
    virtual_netbox_hosts = get_netbox_hosts(
        netbox_virtual_url, cachefiles["fqdn"], headers=headers
    )
    write_to_cache(virtual_netbox_hosts, cachefiles["netbox_virtual"])


def update_netbox_physical_cache():
    logging.info("Updating Netbox physical cache")
    netbox_physical_url = f"{config['netbox']['url']}/dcim/devices/"
    headers = {"Authorization": f"Token {config['netbox']['api_token']}"}
    physical_netbox_hosts = get_netbox_hosts(
        netbox_physical_url, cachefiles["fqdn"], headers=headers
    )
    write_to_cache(physical_netbox_hosts, cachefiles["netbox_physical"])


def update_selected_caches(*update_functions):
    for update_function in update_functions:
        update_function()


@click.command()
@click.option("--openstack", is_flag=True, help="Update OpenStack cache.")
@click.option("--known-hosts", is_flag=True, help="Update known hosts cache.")
@click.option(
    "--local-known-hosts", is_flag=True, help="Update local known hosts cache."
)
@click.option("--netbox-virtual", is_flag=True, help="Update Netbox virtual cache.")
@click.option("--netbox-physical", is_flag=True, help="Update Netbox physical cache.")
def main(openstack, known_hosts, local_known_hosts, netbox_virtual, netbox_physical):
    selected_updates = []
    if openstack:
        selected_updates.append(update_openstack_cache)
    if known_hosts:
        selected_updates.append(update_known_hosts_cache)
    if local_known_hosts:
        selected_updates.append(update_local_known_hosts_cache)
    if netbox_virtual:
        selected_updates.append(update_netbox_virtual_cache)
    if netbox_physical:
        selected_updates.append(update_netbox_physical_cache)

    if not selected_updates:
        selected_updates = [
            update_openstack_cache,
            update_known_hosts_cache,
            update_local_known_hosts_cache,
            update_netbox_virtual_cache,
            update_netbox_physical_cache,
        ]

    update_selected_caches(*selected_updates)
    merge_cachefiles()
    logging.info("All done!")


if __name__ == "__main__":
    main()
