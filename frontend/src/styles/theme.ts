import { createTheme, type MantineColorsTuple } from '@mantine/core'

const accent: MantineColorsTuple = [
  '#e5fbf5',
  '#c8f1e6',
  '#9be5d4',
  '#6cd9c1',
  '#43cfb1',
  '#2cc9a6',
  '#1cc7a0',
  '#0aaf8b',
  '#009c7c',
  '#00866b',
]

export const theme = createTheme({
  primaryColor: 'accent',
  colors: { accent },
  defaultRadius: 'md',
  fontFamily: 'var(--font-sans)',
  fontFamilyMonospace: 'var(--font-mono)',
  headings: { fontFamily: 'var(--font-sans)' },
  white: '#e3ecf7',
  black: '#0b1426',
  cursorType: 'pointer',
  components: {
    Card: {
      defaultProps: {
        withBorder: true,
        bg: 'var(--color-surface)',
      },
    },
    Paper: {
      defaultProps: {
        bg: 'var(--color-surface)',
      },
    },
  },
})
