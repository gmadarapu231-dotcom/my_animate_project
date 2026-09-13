# The CareerOS website

A single-page product site with the working demo embedded in it. Router-free by
design: no History API anywhere, so it runs from `file://`, from any subpath,
and inside a sandboxed frame. The compiled Expo web bundle cannot — it resolves
routes from `location.pathname` and only boots at a host root.

    python3 build.py

assembles three sources into two outputs:

| source | |
| --- | --- |
| `site.html` | the site: copy, layout, and the console the app renders into |
| `app.js` | the app itself — six screens, extracted from `template.html` |
| `../app/src/demoFixtures.json` | responses captured from the running API |

| output | for |
| --- | --- |
| `careeros.html` | a complete document — open it straight from disk |
| `artifact.html` | a bare body, for hosts that supply their own `<head>` |

`build.py` also reads the seeded domain list out of
`careeros/config/taxonomy/domains.yaml`, so the coverage section cannot drift
from the real taxonomy, and mirrors the recorded agent transcript from
`../app/src/demo.ts`.

`template.html` is the app on its own, without the site around it — the source
of `app.js` and useful for looking at the screens in isolation.

Nothing on the page calls a network API: search answers only the recorded
queries and the agent replays one transcript. The live system is the Python API
plus the Expo client in `../app`.
