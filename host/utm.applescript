-- This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
--
-- SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
-- SPDX-License-Identifier: MIT

on run argv
    set action to item 1 of argv
    if action is "create" then set restoreImage to (POSIX file (item 3 of argv)) as alias
    tell application "UTM"
        if action is "create" then
            set vmName to item 2 of argv
            if (count of (virtual machines whose name is vmName)) is not 0 then error "A VM with this name already exists; nothing was changed."
            set ramMiB to (item 4 of argv) as integer
            set cores to (item 5 of argv) as integer
            set diskMiB to (item 6 of argv) as integer
            set vm to make new virtual machine with properties {backend:apple, configuration:{name:vmName, os:macOS, macRecoveryIpsw:restoreImage, memory:ramMiB, cpu cores:cores, directory shares:{}, network interfaces:{{mode:shared}}, drives:{{guest size:diskMiB}}}}
            return id of vm
        end if
        set vm to virtual machine id (item 2 of argv)
        if action is "install" then
            install vm
        else if action is "progress" then
            set p to query install vm
            if installing of p then return "installing"
            return "installed"
        else
            error "Unknown action"
        end if
    end tell
end run
