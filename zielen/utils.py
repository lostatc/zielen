"""A collection of miscellaneous utilities.

Copyright © 2016-2017 Garrett Powell <garrett@gpowell.net>

This file is part of zielen.

zielen is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

zielen is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with zielen.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import atexit
import collections
import shutil
import subprocess
import datetime
from typing import Collection


def shell_cmd(input_cmd: list) -> subprocess.Popen:
    """Run a shell command and terminate it on exit.

    Args:
        input_cmd: The shell command to run, with each argument as an element
            in a list.
    """
    cmd = subprocess.Popen(
        input_cmd, bufsize=1, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True)
    atexit.register(cmd.terminate)
    return cmd


def timestamp_path(path: str, keyword="") -> str:
    """Return a timestamped version of a file path.

    Example:
        >>> timestamp_path("/home/guido/notes.txt", keyword="conflict")
        "/home/guido/notes_conflict-20170219-145503.txt"

    Args:
        path: The file path on which to base the new file path.
        keyword: A string to include in the new file path before the
            timestamp.

    Returns:
        The modified file path.
    """
    keyword += "-" if keyword else keyword
    name, extension = os.path.splitext(path)
    return (
        name
        + "_"
        + keyword
        + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        + extension)


class FactoryDict(collections.defaultdict):
    """A defaultdict that passes the key value into the factory function."""
    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        else:
            return self.default_factory(key)


class DictProperty:
    """A property for the getting and setting of individual dictionary keys."""
    class _Proxy:
        def __init__(self, obj, fget, fset, fdel):
            self._obj = obj
            self._fget = fget
            self._fset = fset
            self._fdel = fdel

        def __getitem__(self, key):
            if self._fget is None:
                raise TypeError("can't read item")
            return self._fget(self._obj, key)

        def __setitem__(self, key, value):
            if self._fset is None:
                raise TypeError("can't set item")
            self._fset(self._obj, key, value)

        def __delitem__(self, key):
            if self._fdel is None:
                raise TypeError("can't delete item")
            self._fdel(self._obj, key)

    def __init__(self, fget=None, fset=None, fdel=None, doc=None):
        self._fget = fget
        self._fset = fset
        self._fdel = fdel
        self.__doc__ = doc

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return self._Proxy(obj, self._fget, self._fset, self._fdel)

    def getter(self, fget):
        return type(self)(fget, self._fset, self._fdel, self.__doc__)

    def setter(self, fset):
        return type(self)(self._fget, fset, self._fdel, self.__doc__)

    def deleter(self, fdel):
        return type(self)(self._fget, self._fset, fdel, self.__doc__)


class ProgressBar:
    """An ascii progress bar for the terminal.

    Attributes:
        coverage: The percentage of the width of the terminal window that the
            progress bar should cover as a decimal between 0 and 1.
        msg: A message to be printed opposite the progress bar.
        r_align: Align the progress bar to the right edge of the screen as
            opposed to the left.
        fill_char: The character that will comprise the filled portion of the
            bar.
        empty_char: The character that will comprise the empty portion of the
            bar.
    """
    def __init__(self, coverage: float, msg="", r_align=True, fill_char="#",
                 empty_char="-") -> None:
        self.coverage = coverage
        self.msg = msg
        self.r_align = r_align
        self.fill_char = fill_char[0]
        self.empty_char = empty_char[0]

    def update(self, fill_amount: float) -> None:
        """Print an updated progress bar.

        Args:
            fill_amount: Fill the bar to this percentage as a decimal between 0
                and 1.
        """
        if fill_amount > 1 or fill_amount < 0:
            raise ValueError("expected a number between 0 and 1")

        term_width = shutil.get_terminal_size()[0]
        bar_length = int(round(term_width * self.coverage))
        filled_length = int(round(bar_length * fill_amount))
        empty_length = bar_length - filled_length
        percent_str = str(round(fill_amount * 100)).rjust(3)
        bar_str = "[{0}] {1}%".format(
            self.fill_char*filled_length + self.empty_char*empty_length,
            percent_str)

        # Truncate input message so that it doesn't overlap with the bar.
        trunc_length = term_width - len(bar_str) - 1
        trunc_msg = self.msg[:trunc_length]

        if self.r_align:
            print(trunc_msg + bar_str.rjust(term_width - len(trunc_msg)),
                  flush=True, end="\r")
        else:
            print(bar_str + trunc_msg.rjust(term_width - len(bar_str)),
                  flush=True, end="\r")


def print_table(headers: list, data: Collection[tuple]) -> None:
    """Print input values in a formatted ascii table.

    All values in the table are left-aligned, and columns are as wide as
    their longest value.

    Args:
        data: The values used to fill the body of the table. Each item in this
            collection represents a row in the table.
        headers: The values to use as column headings.
    """
    column_lengths = []
    for content, header in zip(zip(*data), headers):
        column = [str(item) for item in [*content, header]]
        column_lengths.append(len(max(column, key=len)))

    # Print the table header.
    print(" | ".join([
        "{0:<{1}}".format(name, width)
        for name, width in zip(headers, column_lengths)]))

    # Print the separator between the header and body.
    print("-+-".join(["-"*length for length in column_lengths]))

    # Print the table body.
    for row in data:
        print(" | ".join([
             "{0:<{1}}".format(field, width)
             for field, width in zip(row, column_lengths)]))