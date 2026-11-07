/* Card pager: shows 2 rows of cards per page.
   Cards that a filter/search hides must have data-match="0".
   Usage: const pager = makeCardPager({ grid: "#gridId", card: ".card-class", noun: "loved ones" });
          pager.update(true);   // true = go back to page 1 */
(function () {
    function columnCount(grid) {
        var cols = getComputedStyle(grid).gridTemplateColumns;
        return Math.max(1, cols.split(" ").filter(Boolean).length);
    }

    window.makeCardPager = function (opts) {
        var grid = document.querySelector(opts.grid);
        var rows = opts.rows || 2;
        var noun = opts.noun || "items";
        var page = 1;
        if (!grid) return { update: function () { } };

        var bar = document.createElement("div");
        bar.className = "cp-bar";
        bar.hidden = true;
        grid.insertAdjacentElement("afterend", bar);

        function btn(label, target, extra, disabled) {
            var b = document.createElement("button");
            b.type = "button";
            b.className = "cp-btn" + (extra ? " " + extra : "");
            b.textContent = label;
            b.disabled = !!disabled;
            if (!disabled) b.addEventListener("click", function () { page = target; update(false); });
            return b;
        }

        function pageList(current, total) {
            if (total <= 7) return Array.from({ length: total }, function (_, i) { return i + 1; });
            var out = [1];
            var from = Math.max(2, current - 1);
            var to = Math.min(total - 1, current + 1);
            if (from > 2) out.push("...");
            for (var i = from; i <= to; i++) out.push(i);
            if (to < total - 1) out.push("...");
            out.push(total);
            return out;
        }

        function update(resetPage) {
            if (resetPage) page = 1;
            var all = Array.prototype.slice.call(grid.querySelectorAll(opts.card));
            var matched = all.filter(function (c) { return c.dataset.match !== "0"; });
            var size = columnCount(grid) * rows;
            var pages = Math.max(1, Math.ceil(matched.length / size));
            if (page > pages) page = pages;
            var start = (page - 1) * size;
            var visible = matched.slice(start, start + size);

            all.forEach(function (c) { c.style.display = "none"; });
            visible.forEach(function (c) { c.style.display = ""; });

            bar.textContent = "";
            if (matched.length <= size) { bar.hidden = true; return; }
            bar.hidden = false;

            var summary = document.createElement("span");
            summary.className = "cp-summary";
            summary.textContent = "Showing " + (start + 1) + " to " + (start + visible.length) +
                " of " + matched.length + " " + noun;
            bar.appendChild(summary);

            var controls = document.createElement("div");
            controls.className = "cp-controls";
            controls.appendChild(btn("‹", page - 1, "", page === 1));
            pageList(page, pages).forEach(function (p) {
                if (p === "...") {
                    var dots = document.createElement("span");
                    dots.className = "cp-dots";
                    dots.textContent = "…";
                    controls.appendChild(dots);
                } else {
                    controls.appendChild(btn(String(p), p, p === page ? "active" : "", false));
                }
            });
            controls.appendChild(btn("›", page + 1, "", page === pages));
            bar.appendChild(controls);
        }

        var timer;
        window.addEventListener("resize", function () {
            clearTimeout(timer);
            timer = setTimeout(function () { update(false); }, 150);
        });

        return { update: update };
    };
})();