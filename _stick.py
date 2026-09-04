import os
from pathlib import Path

# ============================================================ chart.js
p = Path("static/ui/chart.js")
s = p.read_text(encoding="utf-8")
N = 0


def sub(old, new, n=1):
    global s, N
    c = s.count(old)
    assert c == n, "expected %d of:\n%s\n-- found %d" % (n, old[:170], c)
    s = s.replace(old, new)
    N += 1


sub('''    if (!this.view || !keepView || !had) {
      const n = this.bars.length;
      this.view = [Math.max(0, n - 220), n];
      this.priceRange = null;
      this.autoScale = true;
    } else {
      // new bars arrived on the right: follow them only if we were at the edge
      const atEdge = this.view[1] >= had - 1;
      if (atEdge) {
        const shift = this.bars.length - had;
        this.view = [this.view[0] + shift, this.bars.length];
      }
    }''',
    '''    if (!this.view || !keepView || !had) {
      const n = this.bars.length;
      this.view = [Math.max(0, n - 220), n];
      this.priceRange = null;
      this.autoScale = true;
    } else if (this.userMoved) {
      // ONCE YOU MOVE THE CHART, IT STAYS WHERE YOU PUT IT.
      // Nothing here touches the view again until Fit. The old rule was "follow
      // the newest bar if the view is at the right edge", but a view panned
      // PAST the last bar also satisfies "at the edge" -- so every poll, two
      // seconds apart, re-anchored view[1] to the bar count and yanked the
      // chart back to the right. It also did that when no new bars had
      // arrived at all, because the re-anchor was unconditional.
    } else {
      // Not moved by hand: follow new bars, and only when there ARE new bars.
      const shift = this.bars.length - had;
      if (shift > 0) this.view = [this.view[0] + shift, this.view[1] + shift];
    }''')

# the flag itself
sub('''    this.hover = null;''',
    '''    this.hover = null;
    // set by any deliberate pan or zoom, cleared by Fit. While true the chart
    // never re-anchors itself to the newest bar.
    this.userMoved = false;''')

sub('''  resetView() {
    const n = this.bars.length;''',
    '''  resetView() {
    this.userMoved = false;          // Fit hands control back to the chart
    const n = this.bars.length;''')

# every deliberate interaction marks it
sub('''  _panX(dxFrac) {''',
    '''  _panX(dxFrac) {
    this.userMoved = true;''')

p.write_text(s, encoding="utf-8")
print("chart.js: %d changes -- the view sticks" % N)
