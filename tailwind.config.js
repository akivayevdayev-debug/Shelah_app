/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/js/**/*.js',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans:   ['Inter', 'sans-serif'],
        // The fallback faces are defined in static/css/tokens.css (audit L-12).
        serif:  ['Cardo', '"Cardo Fallback"', 'serif'],
        hebrew: ['"Ezra SIL"', '"Ezra SIL Fallback"', 'serif'],
      },
      colors: {
        navy: '#002147',
        gold: '#D4AF37',
      },
    },
  },
  safelist: [
    'overflow-y-auto',
    'overflow-x-auto',
    'overflow-auto',
  ],
  plugins: [
    require('@tailwindcss/typography'),
  ],
};
