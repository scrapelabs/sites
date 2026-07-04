module.exports = {
  content: [
    './artifacts/goldenproxies-django/core/templates/**/*.html',
    './artifacts/goldenproxies-django/**/*.py',
  ],
  theme: {
    extend: {
      colors: {
        gold: '#D4AF37',
        'gold-light': '#F0D060',
        'gold-dark': '#B8941F',
      },
      fontFamily: {
        serif: ['Georgia', 'Times New Roman', 'serif'],
      },
    },
  },
};
