# SkillRewind website

The official public landing page for [SkillRewind](https://github.com/alanqoudif/SkillRewind), built with
[Astro](https://astro.build) + TypeScript + Tailwind CSS. This directory is fully independent of the Python
project at the repository root — it has its own `package.json`, lockfile, and build.

## Local development

```bash
cd website
npm ci
npm run dev
```

Open `http://localhost:4321`.

## Build

```bash
npm run build      # outputs to website/dist/
npm run preview    # serve the built output locally
npm run check       # astro check (TypeScript/template diagnostics)
npm run test:claims # scan built HTML for prohibited overreach phrases
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `PUBLIC_SITE_URL` | Canonical site origin used for `<link rel="canonical">`, Open Graph/Twitter meta, and the sitemap. | `https://skillrewind.dev` |

## Deploying to Netlify

1. In Netlify, "Add new site" → "Import an existing project" → connect the `alanqoudif/SkillRewind` GitHub repo.
2. **Base directory:** `website`
3. **Build command:** `npm run build`
4. **Publish directory:** `dist` (relative to the base directory — Netlify will show `website/dist`)
5. Add an environment variable `PUBLIC_SITE_URL` set to the site's real production URL.
6. Deploy. The root-level `netlify.toml` already encodes the base/command/publish settings and security headers,
   so the UI fields above should auto-populate from it — confirm they match before the first deploy.

Do not deploy from this workspace automatically; deployment is a manual step for whoever owns the Netlify site.
