(async function () {
    try {
        const res = await fetch("/auth/me");
        if (!res.ok) return;
        const me = await res.json();
        if (!me.impersonating) return;

        const banner = document.createElement("div");
        banner.style.cssText =
            "position:sticky;top:0;z-index:9999;background:#b45309;color:#fff;" +
            "padding:10px 16px;text-align:center;font-family:sans-serif;font-size:14px;";
        banner.innerHTML =
            `Viewing as <strong>${me.email}</strong> ` +
            `(admin: ${me.impersonator_email}) &mdash; ` +
            `<a href="#" id="exit-impersonation-link" style="color:#fff;text-decoration:underline;">Exit</a>`;
        document.body.prepend(banner);

        document.getElementById("exit-impersonation-link").addEventListener("click", async (e) => {
            e.preventDefault();
            await fetch("/auth/admin/stop-impersonating", { method: "POST" });
            window.location.href = "/dashboard";
        });
    } catch (e) {
        // Fails silently — a missing banner is not worth breaking the page over.
    }
})();
