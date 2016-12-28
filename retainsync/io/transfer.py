"""Run file transfer operations.

Copyright © 2016 Garrett Powell <garrett@gpowell.net>

This file is part of retain-sync.

retain-sync is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

retain-sync is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with retain-sync.  If not, see <http://www.gnu.org/licenses/>.
"""

import sys
import tempfile
from textwrap import indent

from retainsync.util.misc import err, progress_bar, shell_cmd


def rsync_cmd(add_args: list, files=None, exclude=None, msg="") -> None:
    """Run an rsync command and print a status bar.

    Args:
        files:      A list of relative file paths to sync.
        exclude:    A list of relative file paths to exclude from syncing.
        msg:        A message to display opposite the progress bar.
    """
    cmd_args = ["rsync", "--info=progress2"]

    if exclude:
        ex_file = tempfile.NamedTemporaryFile(mode="w+")
        # All file paths must include a leading slash.
        ex_file.write("\n".join(["/" + path.lstrip("/") for path in exclude]))
        ex_file.flush()
        cmd_args.append("--exclude-from=" + ex_file.name)
    if files:
        paths_file = tempfile.NamedTemporaryFile(mode="w+")
        # All file paths must include a leading slash.
        paths_file.write("\n".join(["/" + path.lstrip("/") for path in files]))
        paths_file.flush()
        cmd_args.append("--files-from=" + paths_file.name)

    cmd = shell_cmd(cmd_args + add_args)

    # Print status bar if stdout is a tty.
    if sys.stdout.isatty():
        rsync_bar = progress_bar(0.35, msg)
        for line in cmd.stdout:
            if not line.strip():
                continue
            percent = float(line.split()[1].rstrip("%"))/100
            rsync_bar(percent)
        cmd.wait()
        # Make sure that the progress bar is full once the transfer is
        # completed.
        rsync_bar(1.0)
        print()

    stdout, stderr = cmd.communicate()
    if cmd.returncode != 0:
        err("Error: the file transfer failed to complete")
        # Print the last five lines of rsync's stderr.
        print(indent("\n".join(stderr.splitlines()[-5:]), "    "))
        sys.exit(1)

    if exclude:
        ex_file.close()
    if files:
        paths_file.close()
