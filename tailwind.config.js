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
        serif:  ['Cardo', 'serif'],
        hebrew: ['"Ezra SIL"', 'serif'],
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
