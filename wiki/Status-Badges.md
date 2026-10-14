# Status badges

cronstable serves small SVG images that show the health of one job, or of
every job at once, in the style of the build badges on many README files.
Put one in a README, a wiki page, or a home dashboard, and readers can see
whether a job passes without opening the [web dashboard](Web-Dashboard).

![nightly-backup: passing](https://raw.githubusercontent.com/ptweezy/cronstable/main/docs/img/badge-passing.svg)
![db-vacuum: failing](https://raw.githubusercontent.com/ptweezy/cronstable/main/docs/img/badge-failing.svg)
![weekly-report: overdue](https://raw.githubusercontent.com/ptweezy/cronstable/main/docs/img/badge-overdue.svg)
![cronstable: 12 passing](https://raw.githubusercontent.com/ptweezy/cronstable/main/docs/img/badge-fleet.svg)

## The badge endpoints

| Endpoint | Shows |
|----------|-------|
| `GET /jobs/{name}/badge.svg` | The job's name and its verdict. An unknown job name gets a `404`. |
| `GET /badge.svg` | `cronstable` and a count of the jobs in the most urgent state. |

Both endpoints are on by default whenever the [HTTP API](HTTP-API) is on.
They follow the API's authentication: a daemon that requires a token serves
them only to callers that send it, unless you
[make the badges public](#make-badges-public).

```console
curl http://localhost:8080/jobs/nightly-backup/badge.svg
curl http://localhost:8080/badge.svg
```

## What a job's badge says

A job's badge shows the first message in this table that applies to the job:

| Message | Color | When |
|---------|-------|------|
| `disabled` | gray | The job has `enabled: false`. |
| `paused` | gray | A [runtime pause](Pausing-Jobs) is in effect. |
| `failing` | red | The last run failed. |
| `overdue` | orange | An [SLA check](Late-Run-Detection) is in breach. |
| `passing` | green | The last run succeeded. |
| `cancelled` | gray | The last run was cancelled. |
| `skipped` | gray | A pause held back the last scheduled run. |
| `unknown` | gray | The last run was interrupted before it recorded an outcome. |
| `running` | blue | The job's first run is in progress. |
| `pending` | gray | The job hasn't run yet. |

The badge reads the same state as the dashboard's status column and its
OVERDUE chip. A run that is in progress, or waiting in a
[resource pool](Resource-Pools) queue, leaves the badge on the last run's
result, so the badge of a job that runs every minute holds steady at
`passing` or `failing`.

## What the fleet badge says

The fleet badge counts the jobs whose own badges show the most urgent of
three messages:

1. `failing`, in red, such as `2 failing`.
2. `overdue`, in orange, when no job is failing.
3. `passing`, in green, when no job is failing or overdue.

The fleet badge reads `no status` when no job shows any of the three, for
example before any job has run. It reads `no jobs` when the configuration
has none.

## Query parameters

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `label` | The job's name, or `cronstable` on the fleet badge | The text of the badge's left half: 1 to 64 printable characters. |
| `style` | `flat` | `flat` draws rounded corners and a soft gradient. `flat-square` draws square corners with no gradient. |

A blank, overlong, or unprintable `label`, or a `style` outside those two,
gets a `400` whose JSON error body names the problem.

```console
curl "http://localhost:8080/jobs/nightly-backup/badge.svg?label=nightly%20backup&style=flat-square"
```

![nightly-backup: passing, flat-square style](https://raw.githubusercontent.com/ptweezy/cronstable/main/docs/img/badge-flat-square.svg)

## Embed a badge

The [web dashboard](Web-Dashboard)'s command palette lists
**Copy badge Markdown** for each job, and **Copy fleet badge Markdown**. The
[terminal dashboard](Terminal-Dashboard)'s palette has the same commands.
Each command copies Markdown that shows the badge and links it to the
dashboard. A job's badge links to the job's drawer:

```markdown
[![nightly-backup](https://cron.example.com/jobs/nightly-backup/badge.svg)](https://cron.example.com/#job/nightly-backup)
```

The Markdown uses the address that the dashboard connects to. If readers
reach the daemon at another address, such as a reverse proxy, change the
host before you publish the Markdown.

A page that accepts HTML can use an image tag instead:

```html
<a href="https://cron.example.com/"><img src="https://cron.example.com/badge.svg" alt="cronstable status"></a>
```

## Make badges public

Pages fetch embedded images without your bearer token. GitHub fetches README
images through its image proxy, and a wiki or a home dashboard fetches them
from each reader's browser. On a daemon that requires a token, set
`web.badges.public` so that those fetches succeed:

```yaml
web:
  listen:
    - http://0.0.0.0:8080
  authToken:
    fromEnvVar: CRONSTABLE_WEB_TOKEN
  badges:
    public: true
```

The two badge routes then answer without a token, and every other route
still requires one. A public badge shows a job's name and verdict. Anyone who
can reach the listener can request the badge of any job name and learn from
a `404` whether a job by that name exists, so leave `public` off when job
names are sensitive. GitHub's image proxy fetches from the internet, so a
badge in a GitHub README also needs an address that the internet can reach,
such as a reverse proxy in front of the daemon.

The `view` scope covers both badge routes. A daemon that serves a
[public read-only board](HTTP-API#public-read-only-access-webanonymousscopes)
through `web.anonymousScopes` already serves its badges to everyone.

## Caching

Each badge response carries an `ETag`, a hash of the image's bytes, and
`Cache-Control: no-cache`. The `no-cache` directive tells a cache, such as
the image proxy that GitHub uses for README images, to check with the daemon
before it reuses a stored copy. While the badge is unchanged, that check gets
`304 Not Modified` and no image data.

## Configuration

The `web.badges` option takes a boolean or a map:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Serve both badge routes. |
| `public` | bool | `false` | Serve both badge routes without the bearer token. |

`badges: true` is the same as the default. With `badges: false`, the daemon
doesn't register the badge routes, so a request for a badge gets the same
answer as a request for any unknown path.

## Security headers

A browser that opens a badge URL directly renders the image as a document of
the daemon's origin. Every badge response carries
`Content-Security-Policy: default-src 'none'` and
`X-Content-Type-Options: nosniff`, and the daemon escapes all of the text in
the image. A `web.headers` entry with the same name overrides a default, as
on the dashboard page.

See also: [HTTP Control API](HTTP-API), [Web Dashboard](Web-Dashboard),
[Late-Run Detection (SLA)](Late-Run-Detection), and
[Calendar Export (iCal)](Calendar-Export).
