# Static demo page

A router-free single page that walks through CareerOS using captured real
engine output. It exists because the compiled Expo web bundle resolves routes
from `location.pathname`, so it only boots at a host root — it renders
"Not found" on a subpath, in a sandboxed frame, and from `file://`. This page
touches neither a router nor the History API, so it runs anywhere.

    python3 build.py

writes two files next to the template:

| file | for |
| --- | --- |
| `careeros-demo.html` | a complete document — open it straight from disk |
| `artifact.html` | a bare body, for hosts that supply their own `<head>` |

Data comes from `../app/src/demoFixtures.json` (responses captured from the
running API) plus the recorded agent transcript, which `build.py` mirrors from
`../app/src/demo.ts`. Nothing in the page calls a network API: search answers
only the recorded queries and the agent replays one transcript.

The live app — with a real server and a live agent — is the Expo client in
`../app`. This page is for looking at the output without running anything.
