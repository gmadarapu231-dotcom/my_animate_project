# 12. Client Architecture — Web, iOS and Android

## One component tree, three platforms

`clients/app/` is an Expo + expo-router application that compiles to **iOS,
Android and web** from the same source. Not a shared API layer with three UIs —
the same screens, the same components, the same navigation.

```
clients/app/
  app/                        expo-router: the file tree IS the route tree
    _layout.tsx               theme + session provider + stack
    (tabs)/
      _layout.tsx             Jobs · Today · Pipeline · Insights · Agent
      index.tsx               ranked job list, filters, NL search
      today.tsx               "what should I apply for today?" + learning loop
      applications.tsx        lifecycle, inline status changes
      insights.tsx            funnel + breakdowns
      agent.tsx               the agentic loop, with its tool trace
    job/[id].tsx              full job detail + actions
    settings.tsx              server address + token
    +html.tsx                 web-only HTML shell (title, theme-color, PWA meta)
  src/
    api.ts                    typed client + the API's response types
    hooks.ts                  session context, useQuery, useAction
    settings.ts               persisted server URL + token
    theme.ts                  design tokens, light/dark, deadline colours
    format.ts                 deadline flags, scores, dates
    components/
      ui.tsx                  Screen/Panel/Chip/Button/ScoreTile/ListScreen…
      JobCard.tsx             the job card + the verdict banner
      Table.tsx               compact analytics tables
      Field.tsx               text input
```

Everything is built from `View`/`Text`/`Pressable`, so it renders natively on
device and through `react-native-web` in the browser. There is no
platform-specific screen. The two places the platform shows through are both
deliberate:

- `settings.ts` uses **expo-secure-store** (device keychain) for the API token
  on iOS/Android and falls back to AsyncStorage on web, because SecureStore
  does not exist in a browser. The UI states which one is in use rather than
  implying the browser is as safe as the keychain.
- `defaultBaseUrl()` returns the page origin on web (the API serves the build
  itself) and infers the dev machine's LAN address on device.

### The tradeoff this choice makes

Dense tabular analytics is the one thing `react-native-web` makes harder than
plain React. `components/Table.tsx` renders flex rows with per-column weights
rather than a real `<table>`, which reads fine at phone width and scrolls
horizontally when a caller asks for more columns than fit — but it is not a
sortable, keyboard-navigable desktop data grid. That is the accepted cost of
one component tree. Bottom tabs on a wide desktop window are the same
tradeoff: a mobile pattern, kept for consistency.

## Layout strategy

Responsive by flex, not by breakpoints. Chips and score rows wrap, panels are
capped at `maxWidth: 900` and centred, and no element has a fixed width wider
than a phone. The same Jobs screen at 390pt shows the six scores on two rows
and at 1280px on one.

## Data layer

`useQuery` / `useAction` are hand-rolled — about 100 lines — rather than a
query library. The app needs loading, error, refresh and "don't let a slow
earlier response overwrite a newer one" (a generation counter), and nothing
else. A cache-invalidation policy nobody configured is worse than no cache.

`ApiError` carries `isAuthError` (401) and `isUnavailable` (503) so screens can
say something useful: the Agent tab explains that the server needs
`ANTHROPIC_API_KEY` and that the scheduled pipeline works without it, rather
than showing a bare error.

## What the client must not soften

The clients render the backend's guarantees; they do not get to smooth them
over:

| Guarantee | How the UI keeps it |
|---|---|
| Work-authorization verdicts are assessments | `VerdictBanner` shows the verdict, the JD phrase behind it, and the "AI assessment — verify with employer/recruiter" disclaimer. Not collapsible, not behind a tap. |
| `unknown` is not favourable | `verdictColor` never returns green for `unknown` — it is neutral grey. |
| A failed factuality check blocks a resume | Job detail shows **"BLOCKED — not sendable"** and lists each unsupported claim with its issues, rather than reporting success. |
| Hypotheses are not facts | Insights labels rejection hypotheses as CareerOS guesses, separate from employer-stated reasons. |
| Small samples are not findings | Analytics rows render as "(low sample)" and the funnel repeats the API's note. |
| Deadline-today ranks first | The client **does not re-sort**. Order comes from the API, so the rule lives in exactly one place. |
| The agent is inspectable | The Agent tab shows every tool call marked read / changed / error, plus the withheld-capability list. |

The last row is the general principle: no client-side re-derivation of anything
the backend decides. A second implementation of the ranking rule is a second
place for it to drift.

## Running it

```bash
# once
npm --prefix clients/app install

# web: build, then the API serves it at /
npm --prefix clients/app run export:web
careeros serve

# device / simulator
npm --prefix clients/app start        # then press i, a, or scan with Expo Go
npm --prefix clients/app run typecheck
```

`careeros serve` serves the web build at `/` when `clients/app/dist` exists and
falls back to the built-in single-file dashboard otherwise — which stays
available at `/classic` and needs no npm at all.

### Web serving rules

The app exports as a **single-page bundle** (`web.output: "single"`), so the
server needs a fallback: hashed assets come from `/_expo`, and every other path
returns `index.html` for client-side routing. Without it, a refresh on
`/insights` or a deep link to `/job/1` would 404.

Two rules the fallback must not break, both tested:

- `/api/*`, `/docs`, `/openapi.json` are **reserved**: an unmatched API path
  stays a JSON 404. Serving `index.html` with status 200 would turn every
  client typo into a silent success returning HTML.
- The static handler resolves paths and refuses anything outside the build
  directory.

## Connecting a phone

The API was localhost-only in Phase 1, where "no auth" was defensible. A phone
on the LAN changes that, so `careeros/api/security.py` adds two opt-in controls
whose defaults are the safe ones:

```bash
export CAREEROS_API_TOKEN=$(openssl rand -hex 24)   # required for a phone
careeros serve --host 0.0.0.0
```

Then enter the LAN address and the same token in the app's **Settings** screen.

- **Token unset** → open, for the localhost desktop case. `/api/health` reports
  `auth_required: false` so the client can tell.
- **Token set** → every `/api/*` request needs `Authorization: Bearer <token>`.
  Enforced by middleware, not a per-endpoint dependency: a dependency has to be
  remembered on every new endpoint, and the forgotten one is the one that leaks.
- **CORS** defaults to local dev origins. `CAREEROS_CORS_ORIGINS=*` is honoured
  **only when a token is set** — a wide-open CORS policy on an unauthenticated
  API would let any page the user visits read their career history.

`careeros serve --host 0.0.0.0` with no token prints a warning naming the risk
and the one-line fix, because the failure mode is silent otherwise.

## Verification status — read this honestly

| Target | State |
|---|---|
| **Web** | Verified. Built, served by the real API against a seeded database, every route driven in Chromium with zero console errors, screenshotted at 1280×900 and 390×844. |
| **TypeScript** | `tsc --noEmit` clean across the whole app. |
| **iOS / Android** | **Not run.** There is no iOS or Android simulator in the environment this was built in. The native targets ship verified by typecheck and by a successful Metro bundle, not by execution on a device. |

The native code paths are the same components as the verified web build, and
the two platform-specific branches are small (`settings.ts` storage,
`defaultBaseUrl`). But "compiles and shares verified code" is not "ran on a
phone", and the first `npx expo start` on real hardware is where any remaining
native-only issue will surface — most likely in safe-area insets or keyboard
avoidance, which a browser does not exercise.

## Phase 2 for the clients

1. **Push notifications** — `expo-notifications` plus a server-side scheduler,
   so "deadline today" reaches the phone instead of waiting to be opened. This
   is the main reason the native target exists.
2. **Offline cache** — persist the last job list so the app opens to content on
   a train.
3. **Desktop web refinement** — a sidebar instead of bottom tabs above a width
   breakpoint, and a real sortable grid for analytics.
4. **EAS build + store submission** — `eas build -p ios/android`.
5. **Biometric unlock** for the token on device.
