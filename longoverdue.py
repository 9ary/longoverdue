#!/usr/bin/env python3

import collections
import locale
import os
import re
import subprocess
import sys

import click

# List of services not to restart automatically
NO_AUTORESTART = {"dbus.service": "reboot required",
        "systemd-logind.service": "will log all users out"}

PATH_REGEX = re.compile(r"^(?P<path>.*?)(?: \((?:(?:path dev=(?P<devmajor>\d+),(?P<devminor>\d+)|deleted))\))?$")

locale_encoding = locale.nl_langinfo(locale.CODESET)
euid = os.geteuid()

def color(c, bold = False):
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
            if f.name.startswith("/usr"):
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
def main(ctx):
    """Manage running services that need updating"""
    if ctx.invoked_subcommand is None:
        list_()

@main.command("list")
def list_():
    """List running outdated processes"""
    procs = getprocs()

    uunits = {}
    services = []
    others = {}
    for p in procs:
        if p.uunit != "-":
            if not p.user in uunits:
                uunits[p.user] = []
            uunits[p.user].append((p.uunit, p.command))

        elif p.unit.endswith(".service"):
            services.append((p.unit, p.command))

        else:
            if not p.user in others:
                others[p.user] = []
            others[p.user].append(p.command)

    def warn(desc):
        print(f"{color(15, True)}The following {desc} "
                f"{color(15, True)}may be running outdated code:{color(-1)}")

    def item(name, warning=""):
        if warning is not "":
            warning = f" {color(3)}({warning}){color(-1)}"
        print(f"{color(15)}•{color(-1)} {name}{warning}")

    if services:
        warn("services")
        for s, cmd in sorted(set(services)):
            item(f"{s} ({cmd})", NO_AUTORESTART.get(s, ""))
        print()

    for user, units in uunits.items():
        warn(f"units for user {color(12, True)}{user}")
        for unit, cmd in sorted(set(units)):
            item(f"{unit} ({cmd})")
        print()

    for user, procs in others.items():
        warn(f"processes for user {color(12, True)}{user}")
        for cmd in sorted(set(procs)):
            item(cmd)
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
    prgrep = ["pgrep",  regex]
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
