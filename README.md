# longoverdue
Longoverdue is a script inspired by [overdue](https://github.com/tylerjl/overdue) that looks for running processes that
reference deleted binary file handles and notifies you about them. It allows you to easily restart all affected systemd
services in one go.

```
Usage: longoverdue [OPTIONS] COMMAND [ARGS]...

  Manage running services that need updating

Options:
  --help  Show this message and exit.

Commands:
  info     Get details on a process
  list     List running outdated processes
  restart  Restart outdated services
```

There is an [AUR package](https://aur.archlinux.org/packages/longoverdue/). It provides a post-transaction hook for
pacman to notify you whenever you upgrade packages.
