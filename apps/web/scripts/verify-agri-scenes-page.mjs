/** Quick self-test for newest-page helpers (no vitest in repo). */
import assert from "node:assert/strict";

function newestPageOffset(total, limit) {
  if (!(limit > 0) || !(total > 0)) return 0;
  return Math.max(0, Math.floor(total) - Math.floor(limit));
}

function reverseDescScenesPage(items) {
  return items.length ? [...items].reverse() : [];
}

assert.equal(newestPageOffset(278, 500), 0);
assert.equal(newestPageOffset(800, 500), 300);
assert.equal(newestPageOffset(0, 500), 0);

const desc = [{ date: "2026-09-10" }, { date: "2026-09-01" }, { date: "2025-01-01" }];
const asc = reverseDescScenesPage(desc);
assert.deepEqual(
  asc.map((x) => x.date),
  ["2025-01-01", "2026-09-01", "2026-09-10"],
);
assert.equal(asc[asc.length - 1].date, "2026-09-10");

// total=800 → newest 500 page (via desc) ends at max date after reverse
const fakeDesc800 = Array.from({ length: 500 }, (_, i) => ({
  date: `d-${String(800 - i).padStart(4, "0")}`,
}));
const page = reverseDescScenesPage(fakeDesc800);
assert.equal(page.length, 500);
assert.equal(page[0].date, "d-0301");
assert.equal(page[page.length - 1].date, "d-0800");

console.log("verify-agri-scenes-page: ok");
