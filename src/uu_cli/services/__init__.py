"""One package per UU platform. Each has a cli module with main(argv, prog); one with a login also
has login(ctx, args), which `uu login` runs, and OWN_FILES, the host browser.own_files_only keeps
to its own files (or None). What they share is in common.py."""
