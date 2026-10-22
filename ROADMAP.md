# Roadmap

Ideas for what comes next, roughly by value to students. Each one reuses the shared UU
login and browser session. Nothing here is promised; check that a platform is actually
used at UU, and how it can be reached, before building on it.

## Highest value

- [ ] **Timetable** (`uu timetable today|week`): lectures, rooms and times from UU's
  timetable platform (MyTimetable), with an `.ics` export for your phone's calendar.
- [ ] **Grades and results from Osiris**: official results, credits (ECTS) and study
  progress. A `--watch` mode that notifies you when a new grade appears.
- [ ] **Brightspace deadlines as a calendar feed**: write what `deadlines` already
  collects as `.ics`, on its own or merged with the timetable feed.
- [ ] **What's new across all courses** (`uu brightspace digest`): new files,
  announcements, grades and deadlines since the last run. Suited to a daily cron job
  that sends an email or chat notification.

## Useful, and fairly easy

- [ ] **Osiris course catalogue search**: courses, entry requirements and timeslots from
  the terminal. Mostly public, so no login needed.
- [ ] **Booking study spaces in the library**, if UU's booking system sits behind the UU
  login.
- [ ] **Library catalogue search** (`uu library search <query>`): pairs with
  `uu library open`, and only searches, so publishers' download terms don't come into it.

## Further out

- [ ] **MCP server / agent mode**: read-only commands (deadlines, timetable, course
  files) for AI assistants, so an agent never gets the full CLI.

## Underneath it all

- [ ] **`UU_*` environment variables**, with the `BRIGHTSPACE_*` names kept as aliases,
  now that the login is shared by every platform.
- [ ] **Rename the repo to `uu-cli`**, matching the package and the `uu` command.
- [ ] **Run the offline tests on GitHub Actions** for every pull request.
