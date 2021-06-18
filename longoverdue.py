#!/usr/bin/env python3

import locale
import os
import re
import subprocess
import sys

import click

# List of services not to restart automatically
NO_AUTORESTART = {"dbus.service": "reboot required",
                  "systemd-logind.service": "will log all users out"}

FILE_BLACKLIST = []
EXT_BLACKLIST = [".cache", ".gresource"]

PATH_REGEX = re.compile(r"^(?P<path>.*?)(?: \((?:(?:path dev=(?P<devmajor>\d+),(?P<devminor>\d+)|deleted))\))?$")

locale_encoding = locale.nl_langinfo(locale.CODESET)
euid = os.geteuid()


def color(c, bold=False):
    ret = "\x1b[0;"
    if c >= 0:
        ret += ";38;5;{}".format(c)
    if bold:
        ret += ";1"
    ret += "m"
    return ret


def decode_nuld(nuld):
    return dict((i[0], i[1:]) for i in nuld.strip("\0").split("\0"))


class Process:
    def __init__(self, proc):
        self.pid = proc.get("p")
        self.parent = proc.get("R")
        self.command = proc.get("c")
        self.uid = proc.get("u")
        self.gid = proc.get("g")
        self.user = proc.get("L")
        self.unit = None
        self.uunit = None
        self.files = []


class File:
    def __init__(self, f):
        self.fd = f.get("f")
        self.mode = f.get("a")
        self.lock = f.get("l")
        self.type = f.get("t")
        self.dev = f.get("D")
        self.inode = f.get("i")
        self.name = re.match(PATH_REGEX, f.get("n"))["path"]


def getprocs(pids=None):
    # Get and parse a list of processes that have deleted file handles
    lsof = subprocess.run(["lsof", "-dDEL", "-F0"],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,
                          check=True)
    procs = []
    for l in lsof.stdout.decode(locale_encoding).splitlines():
        if l[0] == "p":
            if procs and not procs[-1].files:
                procs.pop()
            procs.append(Process(decode_nuld(l)))
        elif l[0] == "f":
            f = File(decode_nuld(l))
            if not f.name.startswith("/usr"):
                continue
            if os.path.basename(f.name) in FILE_BLACKLIST:
                continue
            if os.path.splitext(f.name)[1] in EXT_BLACKLIST:
                continue
            procs[-1].files.append(f)
    if procs and not procs[-1].files:
        procs.pop()

    if pids:
        procs = [p for p in procs if p.pid in pids]

    # Find the systemd unit each process belongs to
    pids = [p.pid for p in procs]
    units = subprocess.run(["ps", "-o", "unit=,uunit="] + pids,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL,
                           check=True)
    units = (l.split() for l in units.stdout.decode(locale_encoding).splitlines())
    for proc, (unit, uunit) in zip(procs, units):
        proc.unit = unit
        proc.uunit = uunit

    return procs


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("-v", "--verbose", count=True)
def main(ctx, verbose):
    """Manage running services that need updating"""
    if ctx.invoked_subcommand is None:
        list_()


@main.command("list")
@click.option("-v", "--verbose", count=True)
def list_(verbose):
    """List running outdated processes"""
    kernel = os.uname().release
    if not os.path.exists(f"/usr/lib/modules/{kernel}"):
        print(f"{color(15, True)}The running kernel ({kernel}) was not found in"
              f" the file system, it may have been updated{color(-1)}")
        print()

    procs = getprocs()

    uunits = {}
    services = []
    others = {}
    for p in procs:
        if p.uunit != "-":
            if not p.user in uunits:
                uunits[p.user] = []
            uunits[p.user].append(p)

        elif p.unit.endswith(".service"):
            services.append(p)

        else:
            if not p.user in others:
                others[p.user] = []
            others[p.user].append(p)

    def warn(desc):
        print(f"{color(15, True)}The following {desc} "
              f"{color(15, True)}may be running outdated code:{color(-1)}")

    def item(name, files, warning=""):
        if warning != "":
            warning = f" {color(3)}({warning}){color(-1)}"
        print(f"{color(15)}•{color(-1)} {name}{warning}")
        if verbose:
            for f in files:
                print(f"  {color(15)}•{color(-1)} {f.name}")

    if services:
        warn("services")
        for p in sorted(set(services), key=lambda p: p.unit):
            item(f"{p.unit} ({p.command} ({p.pid}))", p.files, NO_AUTORESTART.get(p.unit, ""))
        print()

    for user, units in uunits.items():
        warn(f"units for user {color(12, True)}{user}")
        for p in sorted(set(units), key=lambda p: p.uunit):
            item(f"{p.uunit} ({p.command} ({p.pid}))", p.files)
        print()

    for user, procs in others.items():
        warn(f"processes for user {color(12, True)}{user}")
        for p in sorted(set(procs), key=lambda p: p.command):
            item(f"{p.command} ({p.pid})", p.files)
        print()


@main.command()
def restart():
    """Restart outdated services"""
    procs = getprocs()

    userservices = set()
    services = set()
    for proc in procs:
        if proc.uunit != "-":
            if proc.uunit.endswith(".service"):
                userservices.add(proc.uunit)
        elif proc.unit.endswith(".service"):
            if proc.unit not in NO_AUTORESTART:
                services.add(proc.unit)

    command = []
    if euid == 0 and services:
        command = ["systemctl", "daemon-reload"]
        print(" ".join(command))
        subprocess.run(command, check=True)

        command = ["systemctl", "restart"] + list(services)
        print(" ".join(command))
        subprocess.run(command, check=True)

    elif euid != 0 and userservices:
        command = ["systemctl", "--user", "daemon-reload"]
        print(" ".join(command))
        subprocess.run(command, check=True)

        command = ["systemctl", "--user", "restart"] + list(userservices)
        print(" ".join(command))
        subprocess.run(command, check=True)


@main.command()
@click.argument("regex")
def info(regex):
    """Get details on a process"""
    prgrep = ["pgrep", regex]
    if euid != 0:
        prgrep.extend(["-u", str(euid)])
    try:
        pids = subprocess.run(prgrep, stdout=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError:
        print(f"{color(15, True)}No process matched {color(12, True)}{regex}"
              f"{color(15, True)}.{color(-1)}")
        sys.exit(1)
    pids = [pid.strip() for pid in pids.stdout.decode(locale_encoding).splitlines()]

    try:
        procs = getprocs(pids)
    except subprocess.CalledProcessError:
        print("Couldn't retrieve process info.")
        sys.exit(1)

    files = sorted(set((f.name for p in procs for f in p.files)))

    if files:
        print(f"{color(12, True)}{regex}{color(15, True)} is using the "
              f"following outdated binaries:{color(-1)}")
        for f in files:
            print(f"{color(15)}•{color(-1)} {f}")
    else:
        print(f"{color(12, True)}{regex}{color(15, True)} appears to be "
              f"running updated binaries.{color(-1)}")


if __name__ == "__main__":
    main()
