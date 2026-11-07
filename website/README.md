# Belaku documentation site

Docusaurus site for the Belaku docs, published to
<https://surikbs-xp.github.io/belaku/>.

## Local development

```bash
npm install
npm run start     # http://localhost:3000/belaku/
```

## Build

```bash
npm run build     # static output in build/
npm run serve     # preview the production build
npm run typecheck
```

The `baseUrl` is `/belaku/`, so the site is only correct under that path
prefix. `onBrokenLinks` and `onBrokenMarkdownLinks` are both set to `throw`, so
a broken link fails the build rather than shipping.

## Writing docs

* Files live in `docs/`; `sidebars.ts` controls the navigation order.
* Use `:::warning[Title]` (bracket form). The bare `:::warning Title` form is
  legacy MDX-v1 compatibility, which `future.v4` disables.
* Link between pages with root-relative routes (`/database/schema`), not `.md`
  paths.
* Mermaid works in fenced ` ```mermaid ` blocks.
* This is MDX: a bare autolink like `<https://x>` is parsed as JSX. Use
  `[https://x](https://x)` instead.
