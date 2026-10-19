import { render } from '@testing-library/react'
import { expect, it } from 'vitest'
import { Markdown } from './markdown'

it('renders GFM, keeps raw HTML as text and drops images', () => {
  const { container } = render(<Markdown text={'# T\n\n**b** `c`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n\n<b>raw</b> ![x](https://evil.test/p.png) [l](https://k.test)'} />)
  expect(container.querySelector('h1')?.textContent).toBe('T')
  expect(container.querySelector('strong')?.textContent).toBe('b')
  expect(container.querySelector('code')?.textContent).toBe('c')
  expect(container.querySelectorAll('td')).toHaveLength(2)
  expect(container.querySelector('input[type="checkbox"]')).not.toBeNull()
  expect(container.querySelector('b')).toBeNull()
  expect(container.querySelector('img')).toBeNull()
  expect(container.querySelector('a')?.getAttribute('rel')).toBe('noreferrer noopener')
})
