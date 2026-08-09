import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

export default defineConfig({
  site: process.env.PUBLIC_SITE_URL || 'https://skillrewind.dev',
  integrations: [tailwind({ applyBaseStyles: false })],
});
