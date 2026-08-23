import { expect, test } from '@playwright/test'
import { discoverRoutes } from './routes'

/**
 * WCAG AA contrast, COMPUTED over the actually-rendered palette.
 *
 * Not eyeballed and not asserted against a table of hex values someone typed:
 * this walks every visible text node in the real browser, reads the computed
 * colour and the effective painted background (walking ancestors until it finds
 * a non-transparent one), and computes the WCAG 2.1 contrast ratio.
 *
 * Thresholds are the AA ones: 4.5:1 for body text, 3.0:1 for large text
 * (>=24px, or >=18.66px when bold).
 */

const CONTRAST_SCRIPT = () => {
  const parse = (value: string): [number, number, number, number] | null => {
    const match = value.match(
      /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+))?\s*\)/,
    )
    if (!match) return null
    return [
      Number(match[1]),
      Number(match[2]),
      Number(match[3]),
      match[4] === undefined ? 1 : Number(match[4]),
    ]
  }

  const luminance = ([r, g, b]: number[]): number => {
    const channel = (c: number) => {
      const v = c / 255
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
    }
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
  }

  const ratio = (a: number[], b: number[]): number => {
    const la = luminance(a)
    const lb = luminance(b)
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
  }

  /** Composite a possibly-translucent colour over an opaque backdrop. */
  const over = (top: number[], bottom: number[]): number[] => {
    const alpha = top[3] ?? 1
    return [0, 1, 2].map((i) => top[i] * alpha + bottom[i] * (1 - alpha))
  }

  const effectiveBackground = (element: Element): number[] => {
    let node: Element | null = element
    let result: number[] = [255, 255, 255]
    const stack: number[][] = []
    while (node) {
      const colour = parse(getComputedStyle(node).backgroundColor)
      if (colour && (colour[3] ?? 1) > 0) {
        stack.push(colour)
        if ((colour[3] ?? 1) === 1) break
      }
      node = node.parentElement
    }
    for (let i = stack.length - 1; i >= 0; i -= 1) {
      result = over(stack[i], result)
    }
    return result
  }

  const problems: {
    text: string
    selector: string
    color: string
    background: string
    ratio: number
    required: number
    fontSize: number
  }[] = []

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  const seen = new Set<Element>()

  while (walker.nextNode()) {
    const textNode = walker.currentNode as Text
    const text = textNode.textContent?.trim() ?? ''
    if (text === '') continue
    const element = textNode.parentElement
    if (!element || seen.has(element)) continue
    seen.add(element)

    const style = getComputedStyle(element)
    if (
      style.visibility === 'hidden' ||
      style.display === 'none' ||
      Number(style.opacity) === 0
    ) {
      continue
    }
    const box = element.getBoundingClientRect()
    // The visually-hidden live region is 1x1 by design and is never read
    // visually; skip anything clipped out of sight.
    if (box.width <= 1 || box.height <= 1) continue

    const foreground = parse(style.color)
    if (!foreground) continue
    const background = effectiveBackground(element)
    const composed = over(foreground, background)

    const fontSize = Number.parseFloat(style.fontSize)
    const weight = Number(style.fontWeight) || 400
    const large = fontSize >= 24 || (fontSize >= 18.66 && weight >= 700)
    const required = large ? 3 : 4.5
    const value = ratio(composed, background)

    if (value + 0.005 < required) {
      problems.push({
        text: text.slice(0, 60),
        selector: `${element.tagName.toLowerCase()}.${element.className}`,
        color: style.color,
        background: `rgb(${background.map(Math.round).join(', ')})`,
        ratio: Number(value.toFixed(2)),
        required,
        fontSize,
      })
    }
  }

  return problems
}

test.describe('WCAG AA contrast, computed', () => {
  test('every rendered text node meets its AA threshold on every route', async ({
    page,
  }) => {
    const routes = await discoverRoutes(page)
    const failures: string[] = []

    for (const route of routes) {
      await page.goto(route.path)
      await page.getByRole('heading', { level: 1 }).waitFor()
      await page.waitForLoadState('networkidle')

      const problems = await page.evaluate(CONTRAST_SCRIPT)
      console.log(
        `contrast — ${route.name} (${route.path}): ${problems.length} text node(s) below threshold`,
      )
      for (const problem of problems) {
        failures.push(
          `${route.path} — "${problem.text}" ${problem.color} on ${problem.background} ` +
            `= ${problem.ratio}:1, needs ${problem.required}:1 (${problem.fontSize}px)`,
        )
      }
    }

    expect(failures, `contrast failures:\n${failures.join('\n')}`).toEqual([])
  })

  test('the focus ring is visible and meets the 3:1 non-text threshold', async ({
    page,
  }) => {
    await page.goto('/conflicts')
    await page.getByRole('table', { name: /conflicts/i }).waitFor()
    await page.keyboard.press('Tab')

    const focusStyle = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement
      const style = getComputedStyle(el)
      return {
        outlineStyle: style.outlineStyle,
        outlineWidth: style.outlineWidth,
        outlineColor: style.outlineColor,
      }
    })

    expect(focusStyle.outlineStyle).not.toBe('none')
    expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThanOrEqual(2)
  })
})
