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
 *
 * The second test does the same thing for the FOCUS RING, which is a non-text
 * UI component and so owes 3:1 (SC 1.4.11). It walks the real tab order rather
 * than sampling one control, because the ring is one colour painted over
 * several shells and the dark header is the shell that breaks it — and it
 * walks that order on EVERY route discovered by ./routes, because a new dark
 * surface arrives with the feature that introduced it, on that feature's route.
 * Neither walk is allowed to stop early: truncation fails the test.
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

/**
 * One focused control, described the way SC 1.4.11 needs it.
 *
 * `background` is the backdrop the RING is painted on, which is not always the
 * control's own background: with `outline-offset: 2px` the ring sits clear of
 * the border box, so what shows through is the shell around the control. The
 * white `aria-current` nav pill inside the dark header is the case that makes
 * the difference — read its own background and a white ring scores 1:1; read
 * the header it floats on and it scores 14.68:1, which is what the eye sees.
 */
interface FocusSample {
  path: string
  label: string
  inHeader: boolean
  position: string
  outlineStyle: string
  outlineWidth: number
  outlineOffset: number
  ring: number[] | null
  background: number[]
  /** First box-shadow layer: the backdrop a floating control paints for itself. */
  shadow: { colour: number[]; spread: number } | null
}

const FOCUS_RING_SCRIPT = (): FocusSample | null => {
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

  const over = (top: number[], bottom: number[]): number[] => {
    const alpha = top[3] ?? 1
    return [0, 1, 2].map((i) => top[i] * alpha + bottom[i] * (1 - alpha))
  }

  /**
   * Walk ancestors until an OPAQUE background is found, compositing every
   * translucent layer found on the way. `background: transparent` is
   * rgba(0,0,0,0) and contributes nothing — it must never be mistaken for
   * white, or a control on the dark header would be scored against a
   * background that is not painted anywhere. White is only the final fallback,
   * used when nothing in the whole chain painted anything at all.
   */
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

  /**
   * The FIRST box-shadow layer, as colour + spread. Only the first layer is
   * read: it is the one painted furthest back, which is the one that acts as a
   * backdrop for the ring.
   */
  const parseShadow = (
    value: string,
  ): { colour: number[]; spread: number } | null => {
    if (!value || value === 'none') return null
    const colour = parse(value)
    if (!colour) return null
    // offset-x, offset-y, blur, spread — in that order, after the colour.
    const lengths = [...value.matchAll(/(-?[\d.]+)px/g)].map((m) => Number(m[1]))
    return { colour, spread: lengths.length >= 4 ? lengths[3] : 0 }
  }

  /** Structural identity, used only to notice when the tab order wraps. */
  const pathOf = (element: Element): string => {
    const parts: string[] = []
    let node: Element | null = element
    while (node && node !== document.documentElement) {
      const parent: Element | null = node.parentElement
      const index = parent ? Array.from(parent.children).indexOf(node) : 0
      parts.unshift(`${node.tagName.toLowerCase()}:${index}`)
      node = parent
    }
    return parts.join('>')
  }

  const element = document.activeElement
  if (
    !element ||
    element === document.body ||
    element === document.documentElement
  ) {
    return null
  }

  const style = getComputedStyle(element)
  const outlineOffset = Number.parseFloat(style.outlineOffset) || 0
  // A positive offset puts the ring clear of the control, on the shell behind
  // it; a zero/negative offset paints it over the control's own box.
  const backdropHost =
    outlineOffset > 0 ? (element.parentElement ?? element) : element
  const background = effectiveBackground(backdropHost)
  const ring = parse(style.outlineColor)
  const label = (element.getAttribute('aria-label') ?? element.textContent ?? '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 40)

  return {
    path: pathOf(element),
    label: label === '' ? `<${element.tagName.toLowerCase()}>` : label,
    inHeader: element.closest('.app-header') !== null,
    position: style.position,
    outlineStyle: style.outlineStyle,
    outlineWidth: Number.parseFloat(style.outlineWidth) || 0,
    outlineOffset,
    shadow: parseShadow(style.boxShadow),
    // Composited, so a translucent ring is judged as it is actually painted.
    ring: ring ? over(ring, background).map((c) => Math.round(c)) : null,
    background: background.map((c) => Math.round(c)),
  }
}

/* WCAG 2.1 relative luminance / contrast, node side (the DOM walk above
 * returns opaque triples, so the maths does not need the browser). */
const relativeLuminance = ([r, g, b]: number[]): number => {
  const channel = (c: number) => {
    const v = c / 255
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

const contrastRatio = (a: number[], b: number[]): number => {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

const rgb = (colour: number[]): string => `rgb(${colour.join(', ')})`

/**
 * Runaway guard for the Tab walk — NOT a step budget.
 *
 * The walk ends on the real end of the tab cycle (focus leaving the document,
 * or returning to a stop already recorded). This number exists only so a focus
 * trap cannot spin the test forever, and HITTING IT FAILS THE TEST rather than
 * quietly truncating the sweep. The value is deliberately far above any real
 * route's stop count so that reaching it means something is wrong, not that the
 * app grew.
 */
const TAB_WALK_RUNAWAY_CAP = 400

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

  test('the focus ring is visible and meets the 3:1 non-text threshold on every shell it lands on, on every route', async ({
    page,
  }) => {
    /*
     * EVERY route, not one. The ring is a single colour painted over whatever
     * shell a control happens to sit in, so the thing that breaks it is a NEW
     * dark surface — and a new dark surface arrives on the route whose feature
     * introduced it, which is almost never the one route a sweep happened to
     * pin. Checking only /conflicts let a real 1.70:1 ring on the proposal
     * detail page ship green. Drive the same enumeration the axe sweep uses so
     * a route added to the app is a route added to this gate, automatically.
     */
    const routes = await discoverRoutes(page)
    const failures: string[] = []
    let totalStops = 0

    for (const route of routes) {
      await page.goto(route.path)
      await page.getByRole('heading', { level: 1 }).waitFor()
      await page.waitForLoadState('networkidle')
      await page.evaluate(() => {
        ;(document.activeElement as HTMLElement | null)?.blur()
      })

      /*
       * Walk the REAL tab order instead of pressing Tab once. One press lands
       * on the skip link, which sits on the white page background — the shell
       * where the ring was never broken. The controls that matter are the nav
       * links a few stops further in, inside the dark header, and whatever the
       * route itself renders after them.
       *
       * The walk ends on the REAL end of the cycle: focus leaving the document,
       * or returning to a stop already recorded. TAB_WALK_RUNAWAY_CAP is only a
       * guard against a focus trap spinning forever, and reaching it is a
       * FAILURE — see the assertion below.
       */
      const samples: FocusSample[] = []
      const seen = new Set<string>()
      let cycleEnded = false
      for (let step = 0; step < TAB_WALK_RUNAWAY_CAP; step += 1) {
        await page.keyboard.press('Tab')
        const sample = await page.evaluate(FOCUS_RING_SCRIPT)
        if (!sample) {
          cycleEnded = true // focus left the document: the cycle is over
          break
        }
        if (seen.has(sample.path)) {
          cycleEnded = true // the tab order wrapped: the cycle is over
          break
        }
        seen.add(sample.path)
        samples.push(sample)
      }

      /*
       * A gate that silently stops checking is not a gate. The old walk stopped
       * at a hardcoded 40 stops and reported green; the real count on
       * /conflicts was 37, and one of the three stops of headroom was consumed
       * by a nav item added during the same session. Truncation now fails, and
       * says which cap it hit.
       */
      expect(
        cycleEnded,
        `${route.path} — the Tab walk hit the TAB_WALK_RUNAWAY_CAP of ` +
          `${TAB_WALK_RUNAWAY_CAP} stops without the tab order wrapping or ` +
          'leaving the document, so the sweep was TRUNCATED and the remaining ' +
          'controls on this route were never checked. Fix the focus trap, or ' +
          'raise TAB_WALK_RUNAWAY_CAP once you have confirmed the route really ' +
          'has that many stops.',
      ).toBe(true)

      const inHeader = samples.filter((sample) => sample.inHeader)
      const elsewhere = samples.filter((sample) => !sample.inHeader)

      // Guard against a vacuous pass, per route: an assertion over an empty
      // walk, or over a walk that never reached the dark header, proves
      // nothing. Both shells exist on every route — the header is in the app
      // shell, and the skip link sits outside it on the page background.
      expect(
        samples.length,
        `${route.path} — the Tab walk reached no focusable control`,
      ).toBeGreaterThan(0)
      expect(
        inHeader.length,
        `${route.path} — the Tab walk never landed on a control inside the ` +
          'dark .app-header, so the shell that fails at 1.70:1 went unchecked',
      ).toBeGreaterThan(0)
      expect(
        elsewhere.length,
        `${route.path} — the Tab walk never left the header, so the light ` +
          'shells went unchecked',
      ).toBeGreaterThan(0)

      totalStops += samples.length

      /*
       * Log one line per DISTINCT ring/backdrop pair rather than per stop: the
       * evidence that matters is which surfaces the ring was actually measured
       * against, and 300-odd identical lines bury it.
       */
      const shells = new Map<string, number>()

      for (const sample of samples) {
        const shell = sample.inHeader ? 'dark header' : 'light shell'
        const where = `${route.path} — ${sample.label} (${shell})`
        if (sample.outlineStyle === 'none' || sample.outlineWidth < 2) {
          failures.push(
            `${where} — no visible ring: outline-style ` +
              `${sample.outlineStyle}, width ${sample.outlineWidth}px`,
          )
          continue
        }
        if (!sample.ring) {
          failures.push(`${where} — outline-color could not be parsed`)
          continue
        }
        /*
         * A control taken out of flow does not sit on its ancestors' paint — it
         * sits on whatever the page happened to draw underneath it, which for
         * the skip link is the dark header (measured: the header's top edge is
         * y=0 whenever the mock banner is absent, and the ring reaches
         * y=58.69). The ancestor walk cannot see that, so the rule for these is
         * stronger: carry an opaque backdrop of your own, at least as wide as
         * the ring reaches.
         */
        if (sample.position === 'absolute' || sample.position === 'fixed') {
          const reach = sample.outlineOffset + sample.outlineWidth
          const shadow = sample.shadow
          if (!shadow || (shadow.colour[3] ?? 1) < 1 || shadow.spread < reach) {
            failures.push(
              `${where} — position:${sample.position}, so its ring is ` +
                `painted over unknown surfaces, but it carries no opaque ` +
                `backdrop reaching ${reach}px (box-shadow spread ` +
                `${shadow?.spread ?? 0}px)`,
            )
            continue
          }
          const backdrop = contrastRatio(shadow.colour.slice(0, 3), sample.ring)
          if (backdrop + 0.005 < 3) {
            failures.push(
              `${where} — its own backdrop ` +
                `${rgb(shadow.colour.slice(0, 3).map(Math.round))} is only ` +
                `${backdrop.toFixed(2)}:1 against the ring ${rgb(sample.ring)}`,
            )
            continue
          }
        }

        const value = contrastRatio(sample.ring, sample.background)
        const key =
          `${rgb(sample.ring)} on ${rgb(sample.background)} ` +
          `= ${value.toFixed(2)}:1 (${shell})`
        shells.set(key, (shells.get(key) ?? 0) + 1)
        // Same 0.005 tolerance as the text sweep: rounding, not slack.
        if (value + 0.005 < 3) {
          failures.push(
            `${where} — ring ${rgb(sample.ring)} on ` +
              `${rgb(sample.background)} = ${value.toFixed(2)}:1, needs 3:1 ` +
              '(WCAG SC 1.4.11, non-text contrast)',
          )
        }
      }

      console.log(
        `focus ring — ${route.name} (${route.path}): ${samples.length} tab ` +
          `stop(s), ${inHeader.length} in the dark header, ` +
          `${elsewhere.length} elsewhere`,
      )
      for (const [key, count] of [...shells.entries()].sort()) {
        console.log(`    ${key} x${count}`)
      }
    }

    // The whole sweep, not just one route: a run that walked nothing at all
    // would otherwise satisfy every assertion above by never entering the loop.
    expect(
      totalStops,
      'the focus-ring sweep recorded no tab stops on any route',
    ).toBeGreaterThan(0)

    expect(
      failures,
      `focus-ring contrast failures:\n${failures.join('\n')}`,
    ).toEqual([])
  })
})
