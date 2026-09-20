# Princeton Events — calendar feeds

Upcoming public Princeton University events, published as subscribable iCalendar (`.ics`)
feeds plus a browsable calendar page. Regenerated automatically once a week.

**Browse:** https://ik301.github.io/princeton-events/

## Feeds

Subscribe to any of these by URL (Google Calendar → Other calendars → **+** → *From URL*):

| Feed | URL | Covers |
|---|---|---|
| Chapel | `https://ik301.github.io/princeton-events/chapel.ics` | Services and concerts at the University Chapel |
| Careers | `https://ik301.github.io/princeton-events/careers.ics` | Center for Career Development workshops, info sessions, employer coffee chats |
| Campus Rec | `https://ik301.github.io/princeton-events/campusrec.ics` | Recreation and fitness programming |
| University | `https://ik301.github.io/princeton-events/university.ics` | University-wide featured events |
| Everything | `https://ik301.github.io/princeton-events/all.ics` | All of the above combined |

Each feed is a separate subscription, so you can show or hide sources independently.

## Sources

- https://chapel.princeton.edu/events
- https://careerdevelopment.princeton.edu/advising-programs/workshops-events
- https://campusrec.princeton.edu/events
- https://www.princeton.edu/feed/events/

All event data belongs to Princeton University and the organizing departments; this repo
just reformats their public listings. Times are published in UTC. `all.ics` and the
per-source `.ics` files use stable per-event UIDs, so re-subscribing or refreshing updates
events in place rather than duplicating them.

## Layout

- `index.html` — self-contained calendar page (month grid + list, per-source toggles, no
  external dependencies); built from the same data as the feeds.
- `*.ics` — the feeds above.
- `scraper/princeton_events.py` — the generator: scrapes each source, parses the listings,
  and writes the `.ics` files and page.

## Notes / limitations

- Refresh cadence is weekly, so brand-new events can take up to a week to appear.
- Calendar clients cache subscriptions; Google can take several hours to pick up a change.
- `my.princeton.edu/events` is behind Princeton SSO and is deliberately not included.
- Multiday and "no time given" events are published as all-day entries.
