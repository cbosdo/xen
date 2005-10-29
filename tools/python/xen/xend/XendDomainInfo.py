#===========================================================================
# This library is free software; you can redistribute it and/or
# modify it under the terms of version 2.1 of the GNU Lesser General Public
# License as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#============================================================================
# Copyright (C) 2004, 2005 Mike Wray <mike.wray@hp.com>
# Copyright (C) 2005 XenSource Ltd
#============================================================================

"""Representation of a single domain.
Includes support for domain construction, using
open-ended configurations.

Author: Mike Wray <mike.wray@hp.com>

"""

import logging
import string
import time
import threading

import xen.lowlevel.xc
from xen.util import asserts
from xen.util.blkif import blkdev_uname_to_file

from xen.xend import image
from xen.xend import scheduler
from xen.xend import sxp
from xen.xend import XendRoot
from xen.xend.XendBootloader import bootloader
from xen.xend.XendError import XendError, VmError
from xen.xend.XendRoot import get_component

import uuid

from xen.xend.xenstore.xstransact import xstransact
from xen.xend.xenstore.xsutil import GetDomainPath, IntroduceDomain

"""Shutdown code for poweroff."""
DOMAIN_POWEROFF = 0

"""Shutdown code for reboot."""
DOMAIN_REBOOT   = 1

"""Shutdown code for suspend."""
DOMAIN_SUSPEND  = 2

"""Shutdown code for crash."""
DOMAIN_CRASH    = 3

"""Shutdown code for halt."""
DOMAIN_HALT     = 4

"""Map shutdown codes to strings."""
shutdown_reasons = {
    DOMAIN_POWEROFF: "poweroff",
    DOMAIN_REBOOT  : "reboot",
    DOMAIN_SUSPEND : "suspend",
    DOMAIN_CRASH   : "crash",
    DOMAIN_HALT    : "halt"
    }

restart_modes = [
    "restart",
    "destroy",
    "preserve",
    "rename-restart"
    ]

STATE_DOM_OK       = 1
STATE_DOM_SHUTDOWN = 2

SHUTDOWN_TIMEOUT = 30

DOMROOT = '/local/domain/'
VMROOT  = '/vm/'

ZOMBIE_PREFIX = 'Zombie-'

"""Minimum time between domain restarts in seconds."""
MINIMUM_RESTART_TIME = 20


xc = xen.lowlevel.xc.new()
xroot = XendRoot.instance()

log = logging.getLogger("xend.XendDomainInfo")
#log.setLevel(logging.TRACE)


## Configuration entries that we expect to round-trip -- be read from the
# config file or xc, written to save-files (i.e. through sxpr), and reused as
# config on restart or restore, all without munging.  Some configuration
# entries are munged for backwards compatibility reasons, or because they
# don't come out of xc in the same form as they are specified in the config
# file, so those are handled separately.
ROUNDTRIPPING_CONFIG_ENTRIES = [
        ('name',         str),
        ('uuid',         str),
        ('ssidref',      int),
        ('vcpus',        int),
        ('vcpu_avail',   int),
        ('cpu_weight',   float),
        ('bootloader',   str),
        ('on_poweroff',  str),
        ('on_reboot',    str),
        ('on_crash',     str)
    ]


#
# There are a number of CPU-related fields:
#
#   vcpus:       the number of virtual CPUs this domain is configured to use.
#   vcpu_avail:  a bitmap telling the guest domain whether it may use each of
#                its VCPUs.  This is translated to
#                <dompath>/cpu/<id>/availability = {online,offline} for use
#                by the guest domain.
#   cpumap:      a list of bitmaps, one for each VCPU, giving the physical
#                CPUs that that VCPU may use.
#   cpu:         a configuration setting requesting that VCPU 0 is pinned to
#                the specified physical CPU.
#
# vcpus and vcpu_avail settings persist with the VM (i.e. they are persistent
# across save, restore, migrate, and restart).  The other settings are only
# specific to the domain, so are lost when the VM moves.
#


def create(config):
    """Create a VM from a configuration.

    @param config    configuration
    @raise: VmError for invalid configuration
    """

    log.debug("XendDomainInfo.create(%s)", config)

    vm = XendDomainInfo(parseConfig(config))
    try:
        vm.construct()
        vm.initDomain()
        vm.storeVmDetails()
        vm.storeDomDetails()
        vm.refreshShutdown()
        return vm
    except:
        log.exception('Domain construction failed')
        vm.destroy()
        raise


def recreate(xeninfo, priv):
    """Create the VM object for an existing domain.  The domain must not
    be dying, as the paths in the store should already have been removed,
    and asking us to recreate them causes problems."""

    log.debug("XendDomainInfo.recreate(%s)", xeninfo)

    assert not xeninfo['dying']

    domid = xeninfo['dom']
    uuid1 = xeninfo['handle']
    xeninfo['uuid'] = uuid.toString(uuid1)
    dompath = GetDomainPath(domid)
    if not dompath:
        raise XendError(
            'No domain path in store for existing domain %d' % domid)

    log.info("Recreating domain %d, UUID %s.", domid, xeninfo['uuid'])
    try:
        vmpath = xstransact.Read(dompath, "vm")
        if not vmpath:
            raise XendError(
                'No vm path in store for existing domain %d' % domid)
        uuid2_str = xstransact.Read(vmpath, "uuid")
        if not uuid2_str:
            raise XendError(
                'No vm/uuid path in store for existing domain %d' % domid)

        uuid2 = uuid.fromString(uuid2_str)

        if uuid1 != uuid2:
            raise XendError(
                'Uuid in store does not match uuid for existing domain %d: '
                '%s != %s' % (domid, uuid2_str, xeninfo['uuid']))

        vm = XendDomainInfo(xeninfo, domid, dompath, True)

    except Exception, exn:
        if priv:
            log.warn(str(exn))

        vm = XendDomainInfo(xeninfo, domid, dompath, True)
        vm.removeDom()
        vm.removeVm()
        vm.storeVmDetails()
        vm.storeDomDetails()

    vm.refreshShutdown(xeninfo)
    return vm


def restore(config):
    """Create a domain and a VM object to do a restore.

    @param config: domain configuration
    """

    log.debug("XendDomainInfo.restore(%s)", config)

    vm = XendDomainInfo(parseConfig(config))
    try:
        vm.construct()
        vm.storeVmDetails()
        vm.createDevices()
        vm.createChannels()
        vm.storeDomDetails()
        return vm
    except:
        vm.destroy()
        raise


def parseConfig(config):
    def get_cfg(name, conv = None):
        val = sxp.child_value(config, name)

        if conv and not val is None:
            try:
                return conv(val)
            except TypeError, exn:
                raise VmError(
                    'Invalid setting %s = %s in configuration: %s' %
                    (name, val, str(exn)))
        else:
            return val


    log.debug("parseConfig: config is %s", config)

    result = {}

    for e in ROUNDTRIPPING_CONFIG_ENTRIES:
        result[e[0]] = get_cfg(e[0], e[1])

    result['memory']    = get_cfg('memory',    int)
    result['mem_kb']    = get_cfg('mem_kb',    int)
    result['maxmem']    = get_cfg('maxmem',    int)
    result['maxmem_kb'] = get_cfg('maxmem_kb', int)
    result['cpu']       = get_cfg('cpu',       int)
    result['image']     = get_cfg('image')

    try:
        if result['image']:
            result['vcpus'] = int(sxp.child_value(result['image'],
                                                  'vcpus', 1))
        else:
            result['vcpus'] = 1
    except TypeError, exn:
        raise VmError(
            'Invalid configuration setting: vcpus = %s: %s' %
            (sxp.child_value(result['image'], 'vcpus', 1), str(exn)))

    result['backend'] = []
    for c in sxp.children(config, 'backend'):
        result['backend'].append(sxp.name(sxp.child0(c)))

    result['device'] = []
    for d in sxp.children(config, 'device'):
        c = sxp.child0(d)
        result['device'].append((sxp.name(c), c))

    # Configuration option "restart" is deprecated.  Parse it, but
    # let on_xyz override it if they are present.
    restart = get_cfg('restart')
    if restart:
        def handle_restart(event, val):
            if result[event] is None:
                result[event] = val

        if restart == "onreboot":
            handle_restart('on_poweroff', 'destroy')
            handle_restart('on_reboot',   'restart')
            handle_restart('on_crash',    'destroy')
        elif restart == "always":
            handle_restart('on_poweroff', 'restart')
            handle_restart('on_reboot',   'restart')
            handle_restart('on_crash',    'restart')
        elif restart == "never":
            handle_restart('on_poweroff', 'destroy')
            handle_restart('on_reboot',   'destroy')
            handle_restart('on_crash',    'destroy')
        else:
            log.warn("Ignoring malformed and deprecated config option "
                     "restart = %s", restart)

    log.debug("parseConfig: result is %s", result)
    return result


def domain_by_name(name):
    # See comment in XendDomain constructor.
    xd = get_component('xen.xend.XendDomain')
    return xd.domain_lookup_by_name_nr(name)

def shutdown_reason(code):
    """Get a shutdown reason from a code.

    @param code: shutdown code
    @type  code: int
    @return: shutdown reason
    @rtype:  string
    """
    return shutdown_reasons.get(code, "?")

def dom_get(dom):
    """Get info from xen for an existing domain.

    @param dom: domain id
    @return: info or None
    """
    try:
        domlist = xc.domain_getinfo(dom, 1)
        if domlist and dom == domlist[0]['dom']:
            return domlist[0]
    except Exception, err:
        # ignore missing domain
        log.debug("domain_getinfo(%d) failed, ignoring: %s", dom, str(err))
    return None


class XendDomainInfo:

    def __init__(self, info, domid = None, dompath = None, augment = False):

        self.info = info

        if not self.infoIsSet('uuid'):
            self.info['uuid'] = uuid.toString(uuid.create())

        if domid is not None:
            self.domid = domid
        elif 'dom' in info:
            self.domid = int(info['dom'])
        else:
            self.domid = None

        self.vmpath  = VMROOT + self.info['uuid']
        self.dompath = dompath

        if augment:
            self.augmentInfo()

        self.validateInfo()

        self.image = None

        self.store_port = None
        self.store_mfn = None
        self.console_port = None
        self.console_mfn = None

        self.state = STATE_DOM_OK
        self.state_updated = threading.Condition()
        self.refresh_shutdown_lock = threading.Condition()


    ## private:

    def augmentInfo(self):
        """Augment self.info, as given to us through {@link #recreate}, with
        values taken from the store.  This recovers those values known to xend
        but not to the hypervisor.
        """
        def useIfNeeded(name, val):
            if not self.infoIsSet(name) and val is not None:
                self.info[name] = val

        params = (("name", str),
                  ("on_poweroff",  str),
                  ("on_reboot",    str),
                  ("on_crash",     str),
                  ("image",        str),
                  ("vcpus",        int),
                  ("vcpu_avail",   int),
                  ("start_time", float))

        from_store = self.gatherVm(*params)

        map(lambda x, y: useIfNeeded(x[0], y), params, from_store)

        device = []
        for c in controllerClasses:
            devconfig = self.getDeviceConfigurations(c)
            if devconfig:
                device.extend(map(lambda x: (c, x), devconfig))
        useIfNeeded('device', device)


    def validateInfo(self):
        """Validate and normalise the info block.  This has either been parsed
        by parseConfig, or received from xc through recreate and augmented by
        the current store contents.
        """
        def defaultInfo(name, val):
            if not self.infoIsSet(name):
                self.info[name] = val()

        try:
            defaultInfo('name',         lambda: "Domain-%d" % self.domid)
            defaultInfo('ssidref',      lambda: 0)
            defaultInfo('on_poweroff',  lambda: "destroy")
            defaultInfo('on_reboot',    lambda: "restart")
            defaultInfo('on_crash',     lambda: "restart")
            defaultInfo('cpu',          lambda: None)
            defaultInfo('cpu_weight',   lambda: 1.0)
            defaultInfo('vcpus',        lambda: int(1))

            self.info['vcpus'] = int(self.info['vcpus'])

            defaultInfo('vcpu_avail',   lambda: (1 << self.info['vcpus']) - 1)
            defaultInfo('bootloader',   lambda: None)
            defaultInfo('backend',      lambda: [])
            defaultInfo('device',       lambda: [])
            defaultInfo('image',        lambda: None)

            self.check_name(self.info['name'])

            if isinstance(self.info['image'], str):
                self.info['image'] = sxp.from_string(self.info['image'])

            # Internally, we keep only maxmem_KiB, and not maxmem or maxmem_kb
            # (which come from outside, and are in MiB and KiB respectively).
            # This means that any maxmem or maxmem_kb settings here have come
            # from outside, and maxmem_KiB must be updated to reflect them.
            # If we have both maxmem and maxmem_kb and these are not
            # consistent, then this is an error, as we've no way to tell which
            # one takes precedence.

            # Exactly the same thing applies to memory_KiB, memory, and
            # mem_kb.

            def discard_negatives(name):
                if self.infoIsSet(name) and self.info[name] < 0:
                    del self.info[name]

            def valid_KiB_(mb_name, kb_name):
                discard_negatives(kb_name)
                discard_negatives(mb_name)
                
                if self.infoIsSet(kb_name):
                    if self.infoIsSet(mb_name):
                        mb = self.info[mb_name]
                        kb = self.info[kb_name]
                        if mb * 1024 == kb:
                            return kb
                        else:
                            raise VmError(
                                'Inconsistent %s / %s settings: %s / %s' %
                                (mb_name, kb_name, mb, kb))
                    else:
                        return self.info[kb_name]
                elif self.infoIsSet(mb_name):
                    return self.info[mb_name] * 1024
                else:
                    return None

            def valid_KiB(mb_name, kb_name):
                result = valid_KiB_(mb_name, kb_name)
                if result is None or result < 0:
                    raise VmError('Invalid %s / %s: %s' %
                                  (mb_name, kb_name, result))
                else:
                    return result

            def delIf(name):
                if name in self.info:
                    del self.info[name]

            self.info['memory_KiB'] = valid_KiB('memory', 'mem_kb')
            delIf('memory')
            delIf('mem_kb')
            self.info['maxmem_KiB'] = valid_KiB_('maxmem', 'maxmem_kb')
            delIf('maxmem')
            delIf('maxmem_kb')

            if not self.info['maxmem_KiB']:
                self.info['maxmem_KiB'] = 1 << 30

            if self.info['maxmem_KiB'] > self.info['memory_KiB']:
                self.info['maxmem_KiB'] = self.info['memory_KiB']

            for (n, c) in self.info['device']:
                if not n or not c or n not in controllerClasses:
                    raise VmError('invalid device (%s, %s)' %
                                  (str(n), str(c)))

            for event in ['on_poweroff', 'on_reboot', 'on_crash']:
                if self.info[event] not in restart_modes:
                    raise VmError('invalid restart event: %s = %s' %
                                  (event, str(self.info[event])))

        except KeyError, exn:
            log.exception(exn)
            raise VmError('Unspecified domain detail: %s' % exn)


    def readVm(self, *args):
        return xstransact.Read(self.vmpath, *args)

    def writeVm(self, *args):
        return xstransact.Write(self.vmpath, *args)

    def removeVm(self, *args):
        return xstransact.Remove(self.vmpath, *args)

    def gatherVm(self, *args):
        return xstransact.Gather(self.vmpath, *args)


    ## public:

    def storeVm(self, *args):
        return xstransact.Store(self.vmpath, *args)


    ## private:

    def readDom(self, *args):
        return xstransact.Read(self.dompath, *args)

    def writeDom(self, *args):
        return xstransact.Write(self.dompath, *args)


    ## public:

    def removeDom(self, *args):
        return xstransact.Remove(self.dompath, *args)


    ## private:

    def storeDom(self, *args):
        return xstransact.Store(self.dompath, *args)


    ## public:

    def completeRestore(self, store_mfn, console_mfn):

        log.debug("XendDomainInfo.completeRestore")

        self.store_mfn = store_mfn
        self.console_mfn = console_mfn

        self.introduceDomain()
        self.storeDomDetails()
        self.refreshShutdown()


    def storeVmDetails(self):
        to_store = {
            'uuid':               self.info['uuid'],

            # XXX
            'memory/target':      str(self.info['memory_KiB'])
            }

        if self.infoIsSet('image'):
            to_store['image'] = sxp.to_string(self.info['image'])

        for k in ['name', 'ssidref', 'on_poweroff', 'on_reboot', 'on_crash',
                  'vcpus', 'vcpu_avail']:
            if self.infoIsSet(k):
                to_store[k] = str(self.info[k])

        log.debug("Storing VM details: %s", to_store)

        self.writeVm(to_store)


    def storeDomDetails(self):
        to_store = {
            'domid':              str(self.domid),
            'vm':                 self.vmpath,
            'name':               self.info['name'],
            'console/limit':      str(xroot.get_console_limit() * 1024),
            'memory/target':      str(self.info['memory_KiB'])
            }

        def f(n, v):
            if v is not None:
                to_store[n] = str(v)

        f('console/port',     self.console_port)
        f('console/ring-ref', self.console_mfn)
        f('store/port',       self.store_port)
        f('store/ring-ref',   self.store_mfn)

        to_store.update(self.vcpuDomDetails())

        log.debug("Storing domain details: %s", to_store)

        self.writeDom(to_store)


    ## private:

    def vcpuDomDetails(self):
        def availability(n):
            if self.info['vcpu_avail'] & (1 << n):
                return 'online'
            else:
                return 'offline'

        result = {}
        for v in range(0, self.info['vcpus']):
            result["cpu/%d/availability" % v] = availability(v)
        return result


    def setDomid(self, domid):
        """Set the domain id.

        @param dom: domain id
        """
        self.domid = domid
        self.storeDom("domid", self.domid)

    def getDomid(self):
        return self.domid

    def setName(self, name):
        self.check_name(name)
        self.info['name'] = name
        self.storeVm("name", name)

    def getName(self):
        return self.info['name']

    def getDomainPath(self):
        return self.dompath


    def getStorePort(self):
        """For use only by image.py and XendCheckpoint.py."""
        return self.store_port


    def getConsolePort(self):
        """For use only by image.py and XendCheckpoint.py"""
        return self.console_port


    def getVCpuCount(self):
        return self.info['vcpus']


    def setVCpuCount(self, vcpus):
        self.info['vcpu_avail'] = (1 << vcpus) - 1
        self.storeVm('vcpu_avail', self.info['vcpu_avail'])
        self.writeDom(self.vcpuDomDetails())


    def getSsidref(self):
        return self.info['ssidref']

    def getMemoryTarget(self):
        """Get this domain's target memory size, in KiB."""
        return self.info['memory_KiB']


    def refreshShutdown(self, xeninfo = None):
        # If set at the end of this method, a restart is required, with the
        # given reason.  This restart has to be done out of the scope of
        # refresh_shutdown_lock.
        restart_reason = None
        
        self.refresh_shutdown_lock.acquire()
        try:
            if xeninfo is None:
                xeninfo = dom_get(self.domid)
                if xeninfo is None:
                    # The domain no longer exists.  This will occur if we have
                    # scheduled a timer to check for shutdown timeouts and the
                    # shutdown succeeded.  It will also occur if someone
                    # destroys a domain beneath us.  We clean up the domain,
                    # just in case, but we can't clean up the VM, because that
                    # VM may have migrated to a different domain on this
                    # machine.
                    self.cleanupDomain()
                    return

            if xeninfo['dying']:
                # Dying means that a domain has been destroyed, but has not
                # yet been cleaned up by Xen.  This state could persist
                # indefinitely if, for example, another domain has some of its
                # pages mapped.  We might like to diagnose this problem in the
                # future, but for now all we do is make sure that it's not us
                # holding the pages, by calling cleanupDomain.  We can't
                # clean up the VM, as above.
                self.cleanupDomain()
                return

            elif xeninfo['crashed']:
                if self.readDom('xend/shutdown_completed'):
                    # We've seen this shutdown already, but we are preserving
                    # the domain for debugging.  Leave it alone.
                    return

                log.warn('Domain has crashed: name=%s id=%d.',
                         self.info['name'], self.domid)

                if xroot.get_enable_dump():
                    self.dumpCore()

                restart_reason = 'crash'

            elif xeninfo['shutdown']:
                if self.readDom('xend/shutdown_completed'):
                    # We've seen this shutdown already, but we are preserving
                    # the domain for debugging.  Leave it alone.
                    return

                else:
                    reason = shutdown_reason(xeninfo['shutdown_reason'])

                    log.info('Domain has shutdown: name=%s id=%d reason=%s.',
                             self.info['name'], self.domid, reason)

                    self.clearRestart()

                    if reason == 'suspend':
                        self.state_set(STATE_DOM_SHUTDOWN)
                        # Don't destroy the domain.  XendCheckpoint will do
                        # this once it has finished.
                    elif reason in ['poweroff', 'reboot']:
                        restart_reason = reason
                    else:
                        self.destroy()

            elif self.dompath is None:
                # We have yet to manage to call introduceDomain on this
                # domain.  This can happen if a restore is in progress, or has
                # failed.  Ignore this domain.
                pass
            else:
                # Domain is alive.  If we are shutting it down, then check
                # the timeout on that, and destroy it if necessary.

                sst = self.readDom('xend/shutdown_start_time')
                if sst:
                    sst = float(sst)
                    timeout = SHUTDOWN_TIMEOUT - time.time() + sst
                    if timeout < 0:
                        log.info(
                            "Domain shutdown timeout expired: name=%s id=%s",
                            self.info['name'], self.domid)
                        self.destroy()
                    else:
                        log.debug(
                            "Scheduling refreshShutdown on domain %d in %ds.",
                            self.domid, timeout)
                        scheduler.later(timeout, self.refreshShutdown)
        finally:
            self.refresh_shutdown_lock.release()

        if restart_reason:
            self.maybeRestart(restart_reason)


    def shutdown(self, reason):
        if not reason in shutdown_reasons.values():
            raise XendError('Invalid reason: %s' % reason)
        self.storeDom("control/shutdown", reason)
        if reason != 'suspend':
            self.storeDom('xend/shutdown_start_time', time.time())


    ## private:

    def clearRestart(self):
        self.removeDom("xend/shutdown_start_time")


    def maybeRestart(self, reason):
        # Dispatch to the correct method based upon the configured on_{reason}
        # behaviour.
        {"destroy"        : self.destroy,
         "restart"        : self.restart,
         "preserve"       : self.preserve,
         "rename-restart" : self.renameRestart}[self.info['on_' + reason]]()


    def renameRestart(self):
        self.restart(True)


    def dumpCore(self):
        """Create a core dump for this domain.  Nothrow guarantee."""
        
        try:
            corefile = "/var/xen/dump/%s.%s.core" % (self.info['name'],
                                                     self.domid)
            xc.domain_dumpcore(dom = self.domid, corefile = corefile)

        except:
            log.exception("XendDomainInfo.dumpCore failed: id = %s name = %s",
                          self.domid, self.info['name'])


    ## public:

    def setMemoryTarget(self, target):
        """Set the memory target of this domain.
        @param target In MiB.
        """
        # Internally we use KiB, but the command interface uses MiB.
        t = target << 10
        self.info['memory_KiB'] = t
        self.storeDom("memory/target", t)


    def update(self, info = None):
        """Update with info from xc.domain_getinfo().
        """

        log.trace("XendDomainInfo.update(%s) on domain %d", info, self.domid)

        if not info:
            info = dom_get(self.domid)
            if not info:
                return
            
        self.info.update(info)
        self.validateInfo()
        self.refreshShutdown(info)

        log.trace("XendDomainInfo.update done on domain %d: %s", self.domid,
                  self.info)


    ## private:

    def state_set(self, state):
        self.state_updated.acquire()
        try:
            if self.state != state:
                self.state = state
                self.state_updated.notifyAll()
        finally:
            self.state_updated.release()


    ## public:

    def waitForShutdown(self):
        self.state_updated.acquire()
        try:
            while self.state == STATE_DOM_OK:
                self.state_updated.wait()
        finally:
            self.state_updated.release()


    def __str__(self):
        s = "<domain"
        s += " id=" + str(self.domid)
        s += " name=" + self.info['name']
        s += " memory=" + str(self.info['memory_KiB'] / 1024)
        s += " ssidref=" + str(self.info['ssidref'])
        s += ">"
        return s

    __repr__ = __str__


    ## private:

    def createDevice(self, deviceClass, devconfig):
        return self.getDeviceController(deviceClass).createDevice(devconfig)


    def reconfigureDevice(self, deviceClass, devid, devconfig):
        return self.getDeviceController(deviceClass).reconfigureDevice(
            devid, devconfig)


    ## public:

    def destroyDevice(self, deviceClass, devid):
        return self.getDeviceController(deviceClass).destroyDevice(devid)


    def getDeviceSxprs(self, deviceClass):
        return self.getDeviceController(deviceClass).sxprs()


    ## private:

    def getDeviceConfigurations(self, deviceClass):
        return self.getDeviceController(deviceClass).configurations()


    def getDeviceController(self, name):
        if name not in controllerClasses:
            raise XendError("unknown device type: " + str(name))

        return controllerClasses[name](self)


    ## public:

    def sxpr(self):
        sxpr = ['domain',
                ['domid',   self.domid],
                ['memory',  self.info['memory_KiB'] / 1024]]

        for e in ROUNDTRIPPING_CONFIG_ENTRIES:
            if self.infoIsSet(e[0]):
                sxpr.append([e[0], self.info[e[0]]])
        
        sxpr.append(['maxmem', self.info['maxmem_KiB'] / 1024])

        if self.infoIsSet('image'):
            sxpr.append(['image', self.info['image']])

        if self.infoIsSet('device'):
            for (_, c) in self.info['device']:
                sxpr.append(['device', c])

        def stateChar(name):
            if name in self.info:
                if self.info[name]:
                    return name[0]
                else:
                    return '-'
            else:
                return '?'

        state = reduce(
            lambda x, y: x + y,
            map(stateChar,
                ['running', 'blocked', 'paused', 'shutdown', 'crashed',
                 'dying']))

        sxpr.append(['state', state])
        if self.infoIsSet('shutdown'):
            reason = shutdown_reason(self.info['shutdown_reason'])
            sxpr.append(['shutdown_reason', reason])
        if self.infoIsSet('cpu_time'):
            sxpr.append(['cpu_time', self.info['cpu_time']/1e9])
        sxpr.append(['vcpus', self.info['vcpus']])
            
        if self.infoIsSet('start_time'):
            up_time =  time.time() - self.info['start_time']
            sxpr.append(['up_time', str(up_time) ])
            sxpr.append(['start_time', str(self.info['start_time']) ])

        if self.store_mfn:
            sxpr.append(['store_mfn', self.store_mfn])
        if self.console_mfn:
            sxpr.append(['console_mfn', self.console_mfn])

        return sxpr


    def getVCPUInfo(self):
        try:
            def filter_cpumap(map, max):
                return filter(lambda x: x >= 0, map[0:max])

            # We include the domain name and ID, to help xm.
            sxpr = ['domain',
                    ['domid',      self.domid],
                    ['name',       self.info['name']],
                    ['vcpu_count', self.info['vcpus']]]

            for i in range(0, self.info['vcpus']):
                info = xc.vcpu_getinfo(self.domid, i)

                sxpr.append(['vcpu',
                             ['number',   i],
                             ['online',   info['online']],
                             ['blocked',  info['blocked']],
                             ['running',  info['running']],
                             ['cpu_time', info['cpu_time'] / 1e9],
                             ['cpu',      info['cpu']],
                             ['cpumap',   filter_cpumap(info['cpumap'],
                                                        self.info['vcpus'])]])

            return sxpr

        except RuntimeError, exn:
            raise XendError(str(exn))
                      

    ## private:

    def check_name(self, name):
        """Check if a vm name is valid. Valid names contain alphabetic characters,
        digits, or characters in '_-.:/+'.
        The same name cannot be used for more than one vm at the same time.

        @param name: name
        @raise: VmError if invalid
        """
        if name is None or name == '':
            raise VmError('missing vm name')
        for c in name:
            if c in string.digits: continue
            if c in '_-.:/+': continue
            if c in string.ascii_letters: continue
            raise VmError('invalid vm name')

        dominfo = domain_by_name(name)
        if not dominfo:
            return
        if self.domid is None:
            raise VmError("VM name '%s' already in use by domain %d" %
                          (name, dominfo.domid))
        if dominfo.domid != self.domid:
            raise VmError("VM name '%s' is used in both domains %d and %d" %
                          (name, self.domid, dominfo.domid))


    def construct(self):
        """Construct the domain.

        @raise: VmError on error
        """

        log.debug('XendDomainInfo.construct: %s %s',
                  self.domid,
                  self.info['ssidref'])

        self.domid = xc.domain_create(
            dom = 0, ssidref = self.info['ssidref'],
            handle = uuid.fromString(self.info['uuid']))

        if self.domid < 0:
            raise VmError('Creating domain failed: name=%s' %
                          self.info['name'])

        self.dompath = GetDomainPath(self.domid)

        self.removeDom()

        # Set maximum number of vcpus in domain
        xc.domain_max_vcpus(self.domid, int(self.info['vcpus']))


    def introduceDomain(self):
        assert self.domid is not None
        assert self.store_mfn is not None
        assert self.store_port is not None
        
        IntroduceDomain(self.domid, self.store_mfn, self.store_port)


    def initDomain(self):
        log.debug('XendDomainInfo.initDomain: %s %s %s',
                  self.domid,
                  self.info['memory_KiB'],
                  self.info['cpu_weight'])

        if not self.infoIsSet('image'):
            raise VmError('Missing image in configuration')

        self.image = image.create(self,
                                  self.info['image'],
                                  self.info['device'])

        if self.info['bootloader']:
            self.image.handleBootloading()

        xc.domain_setcpuweight(self.domid, self.info['cpu_weight'])

        m = self.image.getDomainMemory(self.info['memory_KiB'])
        xc.domain_setmaxmem(self.domid, maxmem_kb = m)
        xc.domain_memory_increase_reservation(self.domid, m, 0, 0)

        cpu = self.info['cpu']
        if cpu is not None and cpu != -1:
            xc.domain_pincpu(self.domid, 0, 1 << cpu)

        self.createChannels()

        channel_details = self.image.createImage()

        self.store_mfn = channel_details['store_mfn']
        if 'console_mfn' in channel_details:
            self.console_mfn = channel_details['console_mfn']

        self.introduceDomain()

        self.createDevices()

        self.info['start_time'] = time.time()


    ## public:

    def cleanupDomain(self):
        """Cleanup domain resources; release devices.  Idempotent.  Nothrow
        guarantee."""

        self.release_devices()

        if self.image:
            try:
                self.image.destroy()
            except:
                log.exception(
                    "XendDomainInfo.cleanup: image.destroy() failed.")
            self.image = None

        try:
            self.removeDom()
        except:
            log.exception("Removing domain path failed.")

        try:
            if not self.info['name'].startswith(ZOMBIE_PREFIX):
                self.info['name'] = ZOMBIE_PREFIX + self.info['name']
        except:
            log.exception("Renaming Zombie failed.")

        self.state_set(STATE_DOM_SHUTDOWN)


    def cleanupVm(self):
        """Cleanup VM resources.  Idempotent.  Nothrow guarantee."""

        try:
            self.removeVm()
        except:
            log.exception("Removing VM path failed.")


    def destroy(self):
        """Cleanup VM and destroy domain.  Nothrow guarantee."""

        log.debug("XendDomainInfo.destroy: domid=%s", self.domid)

        self.cleanupVm()
        if self.dompath is not None:
                self.destroyDomain()


    def destroyDomain(self):
        log.debug("XendDomainInfo.destroyDomain(%s)", self.domid)

        try:
            if self.domid is not None:
                xc.domain_destroy(dom=self.domid)
        except:
            log.exception("XendDomainInfo.destroy: xc.domain_destroy failed.")

        self.cleanupDomain()


    ## private:

    def release_devices(self):
        """Release all domain's devices.  Nothrow guarantee."""

        while True:
            t = xstransact("%s/device" % self.dompath)
            for n in controllerClasses.keys():
                for d in t.list(n):
                    try:
                        t.remove(d)
                    except:
                        # Log and swallow any exceptions in removal --
                        # there's nothing more we can do.
                        log.exception(
                           "Device release failed: %s; %s; %s",
                           self.info['name'], n, d)
            if t.commit():
                break


    def createChannels(self):
        """Create the channels to the domain.
        """
        self.store_port = self.createChannel()
        self.console_port = self.createChannel()


    def createChannel(self):
        """Create an event channel to the domain.
        """
        try:
            return xc.evtchn_alloc_unbound(dom=self.domid, remote_dom=0)
        except:
            log.exception("Exception in alloc_unbound(%d)", self.domid)
            raise


    ## public:

    def createDevices(self):
        """Create the devices for a vm.

        @raise: VmError for invalid devices
        """

        for (n, c) in self.info['device']:
            self.createDevice(n, c)

        if self.image:
            self.image.createDeviceModel()


    def device_create(self, dev_config):
        """Create a new device.

        @param dev_config: device configuration
        """
        dev_type = sxp.name(dev_config)
        devid = self.createDevice(dev_type, dev_config)
#        self.config.append(['device', dev.getConfig()])
        return self.getDeviceController(dev_type).sxpr(devid)


    def device_configure(self, dev_config, devid):
        """Configure an existing device.
        @param dev_config: device configuration
        @param devid:      device id
        """
        deviceClass = sxp.name(dev_config)
        self.reconfigureDevice(deviceClass, devid, dev_config)


    def pause(self):
        xc.domain_pause(self.domid)


    def unpause(self):
        xc.domain_unpause(self.domid)


    ## private:

    def restart(self, rename = False):
        """Restart the domain after it has exited.

        @param rename True if the old domain is to be renamed and preserved,
        False if it is to be destroyed.
        """

        config = self.sxpr()

        if self.readVm('xend/restart_in_progress'):
            log.error('Xend failed during restart of domain %d.  '
                      'Refusing to restart to avoid loops.',
                      self.domid)
            self.destroy()
            return

        self.writeVm('xend/restart_in_progress', 'True')

        now = time.time()
        rst = self.readVm('xend/previous_restart_time')
        log.error(rst)
        if rst:
            rst = float(rst)
            timeout = now - rst
            if timeout < MINIMUM_RESTART_TIME:
                log.error(
                    'VM %s restarting too fast (%f seconds since the last '
                    'restart).  Refusing to restart to avoid loops.',
                    self.info['name'], timeout)
            self.destroy()
            return

        self.writeVm('xend/previous_restart_time', str(now))

        try:
            if rename:
                self.preserveForRestart()
            else:
                self.destroyDomain()
                
            try:
                xd = get_component('xen.xend.XendDomain')
                new_dom = xd.domain_create(config)
                try:
                    new_dom.unpause()
                except:
                    new_dom.destroy()
                    raise
            except:
                log.exception('Failed to restart domain %d.', self.domid)
        finally:
            self.removeVm('xend/restart_in_progress')
            
        # self.configure_bootloader()
        #        self.exportToDB()


    def preserveForRestart(self):
        """Preserve a domain that has been shut down, by giving it a new UUID,
        cloning the VM details, and giving it a new name.  This allows us to
        keep this domain for debugging, but restart a new one in its place
        preserving the restart semantics (name and UUID preserved).
        """
        
        new_name = self.generateUniqueName()
        new_uuid = uuid.toString(uuid.create())
        log.info("Renaming dead domain %s (%d, %s) to %s (%s).",
                 self.info['name'], self.domid, self.info['uuid'],
                 new_name, new_uuid)
        self.release_devices()
        self.info['name'] = new_name
        self.info['uuid'] = new_uuid
        self.vmpath = VMROOT + new_uuid
        self.storeVmDetails()
        self.preserve()


    def preserve(self):
        log.info("Preserving dead domain %s (%d).", self.info['name'],
                 self.domid)
        self.storeDom('xend/shutdown_completed', 'True')
        self.state_set(STATE_DOM_SHUTDOWN)


    # private:

    def generateUniqueName(self):
        n = 1
        while True:
            name = "%s-%d" % (self.info['name'], n)
            try:
                self.check_name(name)
                return name
            except VmError:
                n += 1


    def configure_bootloader(self):
        if not self.info['bootloader']:
            return
        # if we're restarting with a bootloader, we need to run it
        # FIXME: this assumes the disk is the first device and
        # that we're booting from the first disk
        blcfg = None
        # FIXME: this assumes that we want to use the first disk
        dev = sxp.child_value(self.config, "device")
        if dev:
            disk = sxp.child_value(dev, "uname")
            fn = blkdev_uname_to_file(disk)
            blcfg = bootloader(self.info['bootloader'], fn, 1,
                               self.info['vcpus'])
        if blcfg is None:
            msg = "Had a bootloader specified, but can't find disk"
            log.error(msg)
            raise VmError(msg)
        self.config = sxp.merge(['vm', ['image', blcfg]], self.config)


    def send_sysrq(self, key):
        asserts.isCharConvertible(key)

        self.storeDom("control/sysrq", '%c' % key)


    def infoIsSet(self, name):
        return name in self.info and self.info[name] is not None


#============================================================================
# Register device controllers and their device config types.

"""A map from device-class names to the subclass of DevController that
implements the device control specific to that device-class."""
controllerClasses = {}

def addControllerClass(device_class, cls):
    """Register a subclass of DevController to handle the named device-class.
    """
    cls.deviceClass = device_class
    controllerClasses[device_class] = cls


from xen.xend.server import blkif, netif, tpmif, pciif, usbif
addControllerClass('vbd',  blkif.BlkifController)
addControllerClass('vif',  netif.NetifController)
addControllerClass('vtpm', tpmif.TPMifController)
addControllerClass('pci',  pciif.PciController)
addControllerClass('usb',  usbif.UsbifController)
