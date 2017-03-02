"""A class for the 'empty-trash' command.

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

from zielen.basecommand import Command


class EmptyTrashCommand(Command):
    """Delete all files in the remote directory marked for deletion.

    Attributes:
        profile: The currently selected profile.
        local_dir: A LocalSyncDir object representing the local directory.
        dest_dir: A DestSyncDir object representing the destination directory.
        connection: A Connection object representing the remote connection.
    """
    def __init__(self, profile_input: str) -> None:
        super().__init__()
        self.profile = self.select_profile(profile_input)
        self.local_dir = None
        self.dest_dir = None
        self.connection = None

    def main(self) -> None:
        """Run the command."""
        self.setup_profile()

        # Remove files marked for deletion.
        files_deleted = 0
        for rel_path in self.dest_dir.db_file.get_paths(deleted=True):
            try:
                os.remove(os.path.join(self.dest_dir.path, rel_path))
            except FileNotFoundError:
                # The file has already been deleted, but the remote database
                # hasn't yet been updated to reflect the change.
                pass
            else:
                files_deleted += 1
        print("{} files deleted".format(files_deleted))
