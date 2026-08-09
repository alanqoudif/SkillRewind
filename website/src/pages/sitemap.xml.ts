import type { APIRoute } from 'astro';
import { siteUrl } from '../data/site';

export const GET: APIRoute = () => {
  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>${siteUrl}/</loc>
  </url>
</urlset>
`;
  return new Response(body, {
    headers: { 'Content-Type': 'application/xml' },
  });
};
