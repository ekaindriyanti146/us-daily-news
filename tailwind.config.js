/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./layouts/**/*.html",
    "./content/**/*.md",
    "./themes/neo-news/layouts/**/*.html"
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        serif: ['Merriweather', 'Georgia', 'serif'],
      },
      colors: {
        'news-blue': '#0056b3',
        'news-dark': '#1a1a1a',
      }
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}