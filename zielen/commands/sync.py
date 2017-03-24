"""A class for the 'sync' command.

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
import shutil
from typing import Iterable, Set, NamedTuple

from zielen.exceptions import ServerError
from zielen.basecommand import Command
from zielen.util.misc import timestamp_path, rec_scan, symlink_tree
from zielen.io.profile import ProfileExcludeFile
from zielen.io.userdata import TrashDir
from zielen.io.transfer import rclone


class SyncCommand(Command):
    """Redistribute files between the local and remote directories.

    Attributes:
        profile: The currently selected profile.
        local_dir: A LocalSyncDir object representing the local directory.
        dest_dir: A DestSyncDir object representing the destination directory.
        connection: A Connection object representing the remote connection.
    """
    _SelectedPaths = NamedTuple(
        "_SelectedPaths",
        [("remaining_space", int), ("paths", Set[str])])

    _UpdatedPaths = NamedTuple(
        "_UpdatedPaths",
        [("local", Set[str]), ("remote", Set[str]), ("all", Set[str])])

    _DeletedPaths = NamedTuple(
        "_DeletedPaths",
        [("local", Set[str]), ("remote", Set[str]), ("trash", Set[str])])

    def __init__(self, profile_input: str) -> None:
        super().__init__()
        self.profile = self.select_profile(profile_input)

    def main(self) -> None:
        """Run the command.

        Raises:
            UserInputError: The specified profile has already been initialized.
            ServerError: The connection to the remote directory was lost.
        """
        self.setup_profile()

        # Copy exclude pattern file to the remote.
        try:
            shutil.copy(self.profile.ex_file.path, os.path.join(
                self.dest_dir.ex_dir, self.profile.info_file.vals["ID"]))
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

        # Expand globbing patterns.
        self.profile.ex_file.glob(self.local_dir.path)

        # Scan the local and remote databases.
        local_files = self.local_dir.get_paths(rel=True, dirs=False).keys()
        local_dirs = self.local_dir.get_paths(
            rel=True, files=False, symlinks=False).keys()
        remote_files = self.dest_dir.get_paths(rel=True, dirs=False).keys()
        remote_dirs = self.dest_dir.get_paths(
            rel=True, files=False, symlinks=False).keys()
        all_files = local_files | remote_files
        all_dirs = local_dirs | remote_dirs
        all_paths = all_files | all_dirs

        # Remove files from the remote database that were previously marked
        # for deletion and have since been deleted from the remote directory.
        self._cleanup_trash()

        # Determine which files are new since the last sync. This must
        # happen before anything is removed from the databases.
        new_paths = self._compute_added()

        # Sync deletions between the local and remote directories.
        del_paths = self._compute_deleted()
        self._rm_local_files(del_paths.local)
        try:
            self._rm_remote_files(del_paths.remote)
            self._trash_files(del_paths.trash)
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

        # Determine which files have been modified since the last sync and
        # handle syncing conflicts.
        mod_paths = self._compute_modified()
        updated_paths = self._handle_conflicts(
            mod_paths.local | new_paths.local,
            mod_paths.remote | new_paths.remote)

        # Update the remote directory with modified local files.
        self._update_remote(updated_paths.local)

        # Update the remote directory with files that were added to the
        # remote directory directly, and not synced there from a local
        # directory.
        self.dest_dir.db_file.add_paths(
            updated_paths.remote - remote_dirs,
            updated_paths.remote - remote_files)

        # Update symlinks in the local directory so that, if the current sync
        # operation gets interrupted, those files don't get deleted from the
        # remote directory on the next sync operation.
        try:
            symlink_tree(
                self.dest_dir.safe_path, self.local_dir.path,
                updated_paths.remote - remote_dirs,
                updated_paths.remote - remote_files,
                exclude=self.dest_dir.db_file.get_tree(deleted=True))
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

        # Add modified files to the local database if they're not already
        # there, inflating their priority values if that option is set in
        # the config file.
        if self.profile.cfg_file.vals["InflatePriority"]:
            self.profile.db_file.add_inflated(
                updated_paths.all - all_dirs,
                updated_paths.all - all_files)
        else:
            self.profile.db_file.add_paths(
                updated_paths.all - all_dirs,
                updated_paths.all - all_files)

        # At this point, the differences between the two directories have been
        # resolved.

        # Calculate which excluded files are still in the remote directory.
        remote_excluded_files = (
            self.profile.ex_file.rel_files & all_paths)

        # Decide which files and directories to keep in the local directory.
        remaining_space, selected_dirs = self._prioritize_dirs(
            self.profile.cfg_file.vals["StorageLimit"])
        if self.profile.cfg_file.vals["SyncExtraFiles"]:
            remaining_space, selected_files = self._prioritize_files(
                remaining_space, exclude=selected_dirs)
        else:
            selected_files = set()

        # Copy the selected files as well as any excluded files still in the
        # remote directory to the local directory and replace all others
        # with symlinks.
        self._update_local(
            selected_dirs | selected_files | remote_excluded_files)

        # Remove excluded files that are still in the remote directory.
        self._rm_excluded_files(remote_excluded_files)

        # Make sure that file mtimes are updated before the time of the last
        # sync is updated in the info file.
        os.sync()

        # The sync is now complete. Update the time of the last sync in the
        # info file.
        self.profile.info_file.update_synctime()
        self.profile.info_file.write()

    def _cleanup_trash(self) -> None:
        """Clean up files marked for deletion in the remote directory.

        Remove files from the remote database that were previously marked
        for deletion and have since been deleted.
        """
        deleted_trash_files = (
            self.dest_dir.db_file.get_tree(deleted=True).keys()
            - self.dest_dir.get_paths(rel=True).keys())
        self.dest_dir.db_file.rm_paths(deleted_trash_files)

    def _rm_excluded_files(self, excluded_paths: Iterable[str]) -> None:
        """Remove excluded files from the remote directory.

        Remove files from the remote directory only if they've been excluded
        by each client. Also remove them from both databases.

        Args:
            excluded_paths: The paths of excluded files to remove.
        """
        # Expand globbing patterns for each client's exclude pattern file.
        pattern_files = []
        for entry in os.scandir(self.dest_dir.ex_dir):
            pattern_file = ProfileExcludeFile(entry.path)
            pattern_file.glob(self.local_dir.path)
            pattern_files.append(pattern_file)

        rm_files = set()
        for excluded_path in excluded_paths:
            for pattern_file in pattern_files:
                if excluded_path not in pattern_file.rel_files:
                    break
            else:
                # The file was not found in one of the exclude pattern
                # files. Remove it from the remote directory and both
                # databases.
                rm_files.add(excluded_path)

        try:
            self._rm_remote_files(rm_files)
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

    def _update_local(self, update_paths: Iterable[str]) -> None:
        """Update the local directory with remote files.

        Args:
            update_paths: The paths of files and directories to copy from the
                remote directory to the local one. All other files in the local
                directory are replaced with symlinks.
        """
        # Create a set including all the files and directories contained in
        # each directory from the input.
        all_update_paths = set()
        for path in update_paths:
            all_update_paths |= self.profile.db_file.get_tree(path).keys()

        # Don't include excluded files or files not in the database
        # (e.g. user-created symlinks).
        all_paths = (self.local_dir.get_paths(
            rel=True, exclude=self.profile.ex_file.rel_files).keys()
            & self.profile.db_file.get_tree().keys())

        stale_paths = list(all_paths - all_update_paths)

        # Sort the file paths so that a directory's contents always come
        # before the directory.
        stale_paths.sort(key=lambda x: x.count(os.sep), reverse=True)

        # Remove old, unneeded files to make room for new ones.
        for stale_path in stale_paths:
            full_stale_path = os.path.join(self.local_dir.path, stale_path)
            try:
                os.remove(full_stale_path)
            except IsADirectoryError:
                try:
                    os.rmdir(full_stale_path)
                except OSError:
                    # The directory has other files in it.
                    pass

        try:
            symlink_tree(
                self.dest_dir.safe_path, self.local_dir.path,
                self.dest_dir.db_file.get_tree(directory=False),
                self.dest_dir.db_file.get_tree(directory=True),
                exclude=self.dest_dir.db_file.get_tree(deleted=True))

            rclone(
                self.dest_dir.safe_path, self.local_dir.path,
                files=update_paths,
                exclude=self.dest_dir.db_file.get_tree(deleted=True),
                msg="Updating local files...")
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

    def _prioritize_files(self, space_limit: int,
                          exclude=None) -> _SelectedPaths:
        """Calculate which files will stay in the local directory.

        Args:
            space_limit: The amount of space remaining in the directory
                (in bytes). This assumes that all files currently exist in the
                directory as symlinks.
            exclude: An iterable of paths of files and directories to not
                consider when selecting files.

        Returns:
            A named tuple containing a set of paths of files to keep in the
            local directory and the amount of space remaining (in bytes) until
            the storage limit is reached.
        """
        if exclude is None:
            exclude = []

        local_files = self.profile.db_file.get_tree(directory=False)
        file_stats = self.dest_dir.get_paths(
            rel=True, dirs=False, symlinks=False,
            exclude=self.profile.ex_file.rel_files)
        adjusted_priorities = []

        # Adjust directory priorities for size.
        for file_path, file_data in local_files.items():
            for exclude_path in exclude:
                if (os.path.commonpath([file_path, exclude_path])
                        == exclude_path):
                    break
            else:
                # The file is not included in the list of excluded paths.
                file_size = file_stats[file_path].st_blocks * 512
                file_priority = file_data.priority
                if self.profile.cfg_file.vals["AccountForSize"]:
                    try:
                        adjusted_priorities.append((
                            file_path, file_priority / file_size, file_size))
                    except ZeroDivisionError:
                        adjusted_priorities.append((file_path, 0, file_size))
                else:
                    adjusted_priorities.append((
                        file_path, file_priority, file_size))

        # Sort directories by priority.
        adjusted_priorities.sort(key=lambda x: x[1], reverse=True)
        prioritized_files = [
            (path, size) for path, priority, size in adjusted_priorities]

        # Calculate which directories will stay in the local directory.
        selected_files = set()
        # This assumes that all symlinks have a disk usage of one block.
        symlink_size = os.stat(self.local_dir.path).st_blksize
        remaining_space = space_limit
        for file_path, file_size in prioritized_files:
            new_remaining_space = remaining_space - file_size + symlink_size
            if new_remaining_space > 0:
                selected_files.add(file_path)
                remaining_space = new_remaining_space

        return self._SelectedPaths(remaining_space, selected_files)

    def _prioritize_dirs(self, space_limit: int) -> _SelectedPaths:
        """Calculate which directories will stay in the local directory.

        Args:
            space_limit: The amount of space remaining in the directory
                (in bytes).
        Returns:
            A tuple containing a list of paths of directories to keep in the
            local directory and the amount of space remaining (in bytes) until
            the storage limit is reached.
        """
        local_files = self.profile.db_file.get_tree(directory=False)
        local_dirs = self.profile.db_file.get_tree(directory=True)
        dir_stats = self.dest_dir.get_paths(
            rel=True, exclude=self.profile.ex_file.rel_files)
        adjusted_priorities = []

        # Calculate the sizes of each directory and adjust directory priorities
        # for size.
        for dir_path, dir_data in local_dirs.items():
            dir_priority = dir_data.priority
            dir_size = 0
            for sub_path in self.dest_dir.db_file.get_tree(start=dir_path):
                # Get the size of the files in the remote directory, as
                # symlinks in the local directory are not followed.
                dir_size += dir_stats[sub_path].st_blocks * 512

            if self.profile.cfg_file.vals["AccountForSize"]:
                try:
                    adjusted_priorities.append((
                        dir_path, dir_priority / dir_size, dir_size))
                except ZeroDivisionError:
                    adjusted_priorities.append((dir_path, 0, dir_size))
            else:
                adjusted_priorities.append((dir_path, dir_priority, dir_size))

        # Sort directories by priority.
        adjusted_priorities.sort(key=lambda x: x[1], reverse=True)
        prioritized_dirs = [
            path for path, priority, size in adjusted_priorities]
        dir_sizes = {
            path: size for path, priority, size in adjusted_priorities}

        # Select which directories will stay in the local directory.
        selected_dirs = set()
        selected_subdirs = set()
        selected_files = set()
        # Set the initial remaining space assuming that no files will stay
        # in the local directory an that they'll all be symlinks,
        # which should have a disk usage of one block. For evey file that is
        # selected, one block will be added back to the remaining space.
        symlink_size = os.stat(self.local_dir.path).st_blksize
        remaining_space = space_limit - len(local_files) * symlink_size
        for dir_path in prioritized_dirs:
            dir_size = dir_sizes[dir_path]

            if dir_path in selected_subdirs:
                # The current directory is a subdirectory of a directory
                # that has already been selected. Skip it.
                continue

            if dir_size > self.profile.cfg_file.vals["StorageLimit"]:
                # The current directory alone is larger than the storage limit.
                # Skip it.
                continue

            # Find all subdirectories of the current directory that are
            # already in the set of selected files.
            contained_files = set()
            contained_dirs = set()
            subdirs_size = 0
            for subpath, subpath_data in self.profile.db_file.get_tree(
                    start=dir_path).items():
                if subpath_data.directory:
                    contained_dirs.add(subpath)
                else:
                    contained_files.add(subpath)
                if subpath in selected_dirs:
                    subdirs_size += dir_sizes[subpath]

            new_remaining_space = (
                remaining_space
                - dir_size
                + subdirs_size
                + len(contained_files - selected_files) * symlink_size)
            if new_remaining_space > 0:
                # Add the current directory to the set of selected files and
                # remove all of its subdirectories from the set.
                selected_subdirs |= contained_dirs
                selected_files |= contained_files
                selected_dirs -= contained_dirs
                selected_dirs.add(dir_path)
                remaining_space = new_remaining_space

        return self._SelectedPaths(remaining_space, selected_dirs)

    def _update_remote(self, update_paths: Iterable[str]) -> None:
        """Update the remote directory with local files.

        Args:
            update_paths: The relative paths of local files to update the
                remote directory with.
        Raises:
            ServerError: The remote directory is unmounted.
        """
        update_paths = set(update_paths)
        update_files = set(
            update_paths - self.local_dir.get_paths(
                rel=True, files=False, symlinks=False).keys())
        update_dirs = set(
            update_paths - self.local_dir.get_paths(
                rel=True, dirs=False).keys())

        # Copy modified local files to the remote directory, excluding symbolic
        # links.
        try:
            rclone(
                self.local_dir.path, self.dest_dir.safe_path,
                files=update_paths, msg="Updating remote files...")
        except FileNotFoundError:
            raise ServerError(
                "the connection to the remote directory was lost")

        # Add new files to the database and update the time of the last sync
        # for existing ones.
        self.dest_dir.db_file.add_paths(update_files, update_dirs)

    def _handle_conflicts(
            self, local_paths: Iterable[str], remote_paths: Iterable[str]
            ) -> _UpdatedPaths:
        """Handle sync conflicts between local and remote files.

        Conflicts are handled by renaming the file that was modified least
        recently to signify to the user that there was a conflict. These files
        aren't treated specially and are synced just like any other file.

        Args:
            local_paths: The relative paths of local files that have been
                modified since the last sync.
            remote_paths: The relative paths of remote files that have been
                modified since the last sync.

        Returns:
            A named tuple containing two sets of relative paths of files
            that have been modified since the last sync: local ones and
            remote ones.
        """
        local_paths = set(local_paths)
        remote_paths = set(remote_paths)
        conflict_paths = local_paths & remote_paths
        local_mtimes = {
            path: data.st_mtime for path, data
            in self.local_dir.get_paths(rel=True).items()}
        remote_mtimes = {
            path: data.st_mtime for path, data
            in self.dest_dir.get_paths(rel=True).items()}

        new_local_files = set()
        old_local_files = set()
        new_remote_files = set()
        old_remote_files = set()

        try:
            for path in conflict_paths:
                new_path = timestamp_path(path, keyword="conflict")
                if (self.profile.db_file.get_path(path)
                        and self.profile.db_file.get_path(path).directory):
                    # Conflicts are resolved on a file-by-file basis.
                    continue
                elif local_mtimes[path] < remote_mtimes[path]:
                    os.rename(
                        os.path.join(self.local_dir.path, path),
                        os.path.join(self.local_dir.path, new_path))
                    old_local_files.add(path)
                    new_local_files.add(new_path)
                elif remote_mtimes[path] < local_mtimes[path]:
                    try:
                        os.rename(
                            os.path.join(self.dest_dir.safe_path, path),
                            os.path.join(self.dest_dir.safe_path, new_path))
                    except FileNotFoundError:
                        raise ServerError(
                            "the connection to the remote directory was lost")
                    old_remote_files.add(path)
                    new_remote_files.add(new_path)
        finally:
            # Remove outdated file paths from the local database, but don't
            # add new ones. If you do, and the current sync operation is
            # interrupted, then those files will be deleted on the next sync
            # operation. The new file paths are added to the database once
            # the differences between the two directories have been resolved.
            self.profile.db_file.rm_paths(old_local_files | old_remote_files)

            # Update file paths in the remote database.
            self.dest_dir.db_file.rm_paths(old_remote_files)
            self.dest_dir.db_file.add_paths(new_remote_files, [])

        local_mod_paths = local_paths - old_local_files | new_local_files
        remote_mod_paths = remote_paths - old_remote_files | new_remote_files
        return self._UpdatedPaths(
            local_mod_paths, remote_mod_paths,
            local_mod_paths | remote_mod_paths)

    def _compute_added(self) -> _UpdatedPaths:
        """Compute paths of files that have been added since the last sync.

        This method excludes the paths of local symlinks. A file is
        considered to be new if it is not in the database.

        Returns:
            A named tuple containing two sets of relative paths of files
            that have been added since the last sync: local ones and
            remote ones.
        """
        local_new_paths = {
            path for path in self.local_dir.get_paths(
                rel=True, symlinks=False).keys()
            if not self.profile.db_file.get_path(path)}
        remote_new_paths = {
            path for path in self.dest_dir.get_paths(rel=True).keys()
            if not self.dest_dir.db_file.get_path(path)}

        return self._UpdatedPaths(
            local_new_paths, remote_new_paths,
            local_new_paths | remote_new_paths)

    def _compute_modified(self) -> _UpdatedPaths:
        """Compute paths of files that have been modified since the last sync.

        This method excludes the paths of directories and the paths of files
        that are new since the last sync. A file is considered to be
        modified if its mtime is more recent than the time of the last sync
        and it is in the database. Additionally, remote files are considered
        to be modified if the time they were last updated by a sync (stored
        in the remote database) is more recent than the time of the last sync.

        Returns:
            A named tuple containing two sets of relative paths of files
            that have been modified since the last sync: local ones and
            remote ones.
        """
        last_sync = self.profile.info_file.vals["LastSync"]

        # Only include file paths that are in the database to exclude files
        # that are new since the last sync.
        local_mtimes = (
            (path, data.st_mtime) for path, data
            in self.local_dir.get_paths(rel=True, dirs=False).items())
        remote_mtimes = (
            (path, data.st_mtime) for path, data
            in self.dest_dir.get_paths(rel=True, dirs=False).items())

        local_mod_paths = {
            path for path, mtime in local_mtimes
            if mtime > last_sync and self.profile.db_file.get_path(path)}
        remote_mod_paths = {
            path for path, mtime in remote_mtimes
            if mtime > last_sync and self.dest_dir.db_file.get_path(path)}

        remote_mod_paths |= self.dest_dir.db_file.get_tree(
            deleted=False, directory=False, min_lastsync=last_sync).keys()

        return self._UpdatedPaths(
            local_mod_paths, remote_mod_paths,
            local_mod_paths | remote_mod_paths)

    def _compute_deleted(self) -> _DeletedPaths:
        """Compute files that need to be deleted to sync the two directories.

        A file needs to be deleted if it is found in the local database but
        not in either the local or remote directory. A file is marked for
        deletion if it needs to be deleted from the remote directory but is
        not found in any of the trash directories.

        Returns:
            A named tuple containing three sets of relative file paths: local
            files to be deleted, remote files to be deleted and remote files to
            be marked for deletion.
        """
        local_paths = self.local_dir.get_paths(rel=True).keys()
        remote_paths = self.dest_dir.get_paths(rel=True).keys()
        known_paths = self.profile.db_file.get_tree().keys()

        # Compute files that need to be deleted.
        local_del_paths = known_paths - remote_paths
        remote_del_paths = known_paths - local_paths

        # Compute files to be marked for deletion.
        trash_paths = set()
        if not self.profile.cfg_file.vals["DeleteAlways"]:
            trash_dir = TrashDir(self.profile.cfg_file.vals["TrashDirs"])
            for path in remote_del_paths:
                dest_path = os.path.join(self.dest_dir.safe_path, path)
                try:
                    if (os.path.isfile(dest_path)
                            and not trash_dir.check_file(dest_path)):
                        trash_paths.add(path)
                except IsADirectoryError:
                    # Directories shouldn't be marked as deleted explicitly.
                    # The database automatically marks directories as deleted
                    # when all their files are.
                    continue
            remote_del_paths -= trash_paths

        return self._DeletedPaths(
            local_del_paths, remote_del_paths, trash_paths)

    def _rm_local_files(self, paths: Iterable[str]) -> None:
        """Delete local files and remove them from both databases.

        Args:
            paths: The relative paths of files to remove.
        """
        deleted_paths = []

        # Make sure that the database always gets updated with whatever files
        # have been deleted.
        try:
            for path in paths:
                full_path = os.path.join(self.local_dir.path, path)
                try:
                    os.remove(full_path)
                except IsADirectoryError:
                    shutil.rmtree(full_path)
                deleted_paths.append(path)
        finally:
            # If a deletion from another client was already synced to the
            # server, then that file path should have already been removed
            # from the remote database. However, they user may have manually
            # deleted files from the remote directory since the last sync.
            self.dest_dir.db_file.rm_paths(deleted_paths)
            self.profile.db_file.rm_paths(deleted_paths)

    def _rm_remote_files(self, paths: Iterable[str]) -> None:
        """Delete remote files and remove them from the databases.

        Args:
            paths: The relative paths of files to remove.
        """
        deleted_paths = []

        # Make sure that the database always gets updated with whatever files
        # have been deleted.
        try:
            for path in paths:
                full_path = os.path.join(self.dest_dir.safe_path, path)
                try:
                    os.remove(full_path)
                except IsADirectoryError:
                    shutil.rmtree(full_path)
                deleted_paths.append(path)
        finally:
            self.profile.db_file.rm_paths(deleted_paths)
            self.dest_dir.db_file.rm_paths(deleted_paths)

    def _trash_files(self, paths: Iterable[str]) -> None:
        """Mark files in the remote directory for deletion.

        This involves renaming the file to signify its state to the user,
        removing it from the local database and updating its entry in the
        remote database to signify its state to the program.

        Args:
            paths: The relative paths of files to mark for deletion.
        """
        new_file_paths = set()
        new_dir_paths = set()
        for path in paths:
            new_path = timestamp_path(path, keyword="deleted")
            # Separate paths into files and directories so that they can be
            # marked accordingly when they are re-added to the database.
            if (self.dest_dir.db_file.get_path(path)
                    and self.dest_dir.db_file.get_path(path).directory):
                new_dir_paths.add(new_path)
            else:
                new_file_paths.add(new_path)

        old_renamed_paths = set()
        new_renamed_paths = set()

        # Make sure that the database always gets updated with whatever files
        # have been renamed.
        try:
            for old_path, new_path in zip(
                    paths, new_file_paths | new_dir_paths):
                os.rename(
                    os.path.join(self.dest_dir.safe_path, old_path),
                    os.path.join(self.dest_dir.safe_path, new_path))
                old_renamed_paths.add(old_path)
                new_renamed_paths.add(new_path)
        finally:
            self.profile.db_file.rm_paths(old_renamed_paths)
            self.dest_dir.db_file.rm_paths(old_renamed_paths)
            self.dest_dir.db_file.add_paths(
                new_file_paths & new_renamed_paths,
                new_dir_paths & new_renamed_paths, deleted=True)
