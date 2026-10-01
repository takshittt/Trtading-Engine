/** The dashboard half of the shared ladder-math contract.
 *
 * Reads the same vectors the backend asserts against
 * (backend/tests/ladder_math_vectors.json). If this file and core/exits.py ever
 * disagree, one of these two suites fails — which is the only thing standing
 * between a preview and the number the engine will actually trade.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { computeSl, computeTarget, offsetOf } from './ladderMath'

const here = dirname(fileURLToPath(import.meta.url))
const vectors = JSON.parse(readFileSync(
  resolve(here, '../../backend/tests/ladder_math_vectors.json'), 'utf8'))

interface TargetCase {
  name: string
  inst: { target_mode: string; target_value: number; target_chain_pct?: number }
  entry: number
  prev: number | null
  expect_target: number
  expect_anchor: number
}
interface SlCase {
  name: string
  inst: { sl_enabled: boolean; sl_mode: string; sl_value: number }
  entry: number
  expect_sl: number
}

describe('computeTarget matches the shared vectors', () => {
  for (const c of vectors.target as TargetCase[]) {
    it(c.name, () => {
      const { target, anchor } = computeTarget(c.inst, c.entry, c.prev)
      expect(target).toBeCloseTo(c.expect_target, 4)
      expect(anchor).toBeCloseTo(c.expect_anchor, 4)
    })
  }
})

describe('computeSl matches the shared vectors', () => {
  for (const c of vectors.sl as SlCase[]) {
    it(c.name, () => {
      expect(computeSl(c.inst, c.entry)).toBeCloseTo(c.expect_sl, 4)
    })
  }
})

describe('invariants the preview must not lose', () => {
  it('a chained target is never at or below its own entry', () => {
    const inst = { target_mode: 'points', target_value: 200, target_chain_pct: 100 }
    for (const prev of [14000, 15000, 15389, 15390, 15391, 16000]) {
      expect(computeTarget(inst, 15390, prev).target).toBeGreaterThan(15390)
    }
  })

  it('a disabled stop is exactly zero, the engine\'s "no stop"', () => {
    expect(computeSl({ sl_enabled: false, sl_mode: 'points', sl_value: 200 }, 15390)).toBe(0)
  })

  it('percent offsets are computed on the anchor, not the entry', () => {
    expect(offsetOf('percent', 2, 1100)).toBeCloseTo(22, 6)
    expect(offsetOf('points', 2, 1100)).toBe(2)
  })
})
