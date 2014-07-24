#!/usr/bin/python

#
# Copyright 2012 Canonical Ltd.
#
# Authors:
#  James Page <james.page@ubuntu.com>
#

import glob
import os
import shutil
import sys

import ceph
from charmhelpers.core.hookenv import (
    log,
    ERROR,
    config,
    relation_ids,
    related_units,
    relation_get,
    Hooks,
    UnregisteredHookError,
    service_name
)
from charmhelpers.core.host import (
    umount,
    mkdir
)
from charmhelpers.fetch import (
    add_source,
    apt_install,
    apt_update,
    filter_installed_packages,
)

from utils import (
    render_template,
    get_host_ip,
)

from charmhelpers.contrib.openstack.alternatives import install_alternative
from charmhelpers.contrib.network.ip import is_ipv6

hooks = Hooks()


def install_upstart_scripts():
    # Only install upstart configurations for older versions
    if ceph.get_ceph_version() < "0.55.1":
        for x in glob.glob('files/upstart/*.conf'):
            shutil.copy(x, '/etc/init/')


@hooks.hook('install')
def install():
    add_source(config('source'), config('key'))
    apt_update(fatal=True)
    apt_install(packages=ceph.PACKAGES, fatal=True)
    install_upstart_scripts()


def emit_cephconf():
    mon_hosts = get_mon_hosts()
    log('Monitor hosts are ' + repr(mon_hosts))

    cephcontext = {
        'auth_supported': get_auth(),
        'mon_hosts': ' '.join(mon_hosts),
        'fsid': get_fsid(),
        'version': ceph.get_ceph_version(),
        'osd_journal_size': config('osd-journal-size'),
        'use_syslog': str(config('use-syslog')).lower(),
        'ceph_public_network': config('ceph-public-network'),
        'ceph_cluster_network': config('ceph-cluster-network'),
    }
    # Install ceph.conf as an alternative to support
    # co-existence with other charms that write this file
    charm_ceph_conf = "/var/lib/charm/{}/ceph.conf".format(service_name())
    mkdir(os.path.dirname(charm_ceph_conf))
    with open(charm_ceph_conf, 'w') as cephconf:
        cephconf.write(render_template('ceph.conf', cephcontext))
    install_alternative('ceph.conf', '/etc/ceph/ceph.conf',
                        charm_ceph_conf, 90)

JOURNAL_ZAPPED = '/var/lib/ceph/journal_zapped'


@hooks.hook('config-changed')
def config_changed():
    # Pre-flight checks
    if config('osd-format') not in ceph.DISK_FORMATS:
        log('Invalid OSD disk format configuration specified', level=ERROR)
        sys.exit(1)

    e_mountpoint = config('ephemeral-unmount')
    if (e_mountpoint and ceph.filesystem_mounted(e_mountpoint)):
        umount(e_mountpoint)

    osd_journal = config('osd-journal')
    if (osd_journal and not os.path.exists(JOURNAL_ZAPPED)
            and os.path.exists(osd_journal)):
        ceph.zap_disk(osd_journal)
        with open(JOURNAL_ZAPPED, 'w') as zapped:
            zapped.write('DONE')

    if ceph.is_bootstrapped():
        log('ceph bootstrapped, rescanning disks')
        emit_cephconf()
        for dev in get_devices():
            ceph.osdize(dev, config('osd-format'),
                        config('osd-journal'), config('osd-reformat'))
        ceph.start_osds(get_devices())


def get_mon_hosts():
    hosts = []
    for relid in relation_ids('mon'):
        for unit in related_units(relid):
            addr = relation_get('ceph-public-address', unit, relid) or \
                get_host_ip(relation_get('private-address', unit, relid))
            if addr is not None:
                if is_ipv6(addr):
                    hosts.append('[{}]:6789'.format(addr))
                else:
                    hosts.append('{}:6789'.format(addr))
    hosts.sort()
    return hosts


def get_fsid():
    return get_conf('fsid')


def get_auth():
    return get_conf('auth')


def get_conf(name):
    for relid in relation_ids('mon'):
        for unit in related_units(relid):
            conf = relation_get(name,
                                unit, relid)
            if conf:
                return conf
    return None


def reformat_osd():
    if config('osd-reformat'):
        return True
    else:
        return False


def get_devices():
    if config('osd-devices'):
        return config('osd-devices').split(' ')
    else:
        return []


@hooks.hook('mon-relation-changed',
            'mon-relation-departed')
def mon_relation():
    bootstrap_key = relation_get('osd_bootstrap_key')
    if get_fsid() and get_auth() and bootstrap_key:
        log('mon has provided conf- scanning disks')
        emit_cephconf()
        ceph.import_osd_bootstrap_key(bootstrap_key)
        for dev in get_devices():
            ceph.osdize(dev, config('osd-format'),
                        config('osd-journal'), config('osd-reformat'))
        ceph.start_osds(get_devices())
    else:
        log('mon cluster has not yet provided conf')


@hooks.hook('upgrade-charm')
def upgrade_charm():
    if get_fsid() and get_auth():
        emit_cephconf()
    install_upstart_scripts()
    apt_install(packages=filter_installed_packages(ceph.PACKAGES),
                fatal=True)


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
