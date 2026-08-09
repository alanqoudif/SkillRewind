/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,ts,jsx,tsx,md,mdx}'],
  darkMode: 'media',
  theme: {
    extend: {
      colors: {
        bg: '#0b0c0e',
        surface: '#121317',
        surface2: '#191b20',
        border: '#26292f',
        ink: '#e9eaec',
        muted: '#9aa0aa',
        recorded: '#5b8dee',
        inferred: '#e0a730',
        confirmed: '#3ec776',
        rejected: '#c96a6a',
        unresolved: '#8a8f98',
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};
