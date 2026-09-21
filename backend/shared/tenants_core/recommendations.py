"""A small rules engine over whatever tenant metrics are already available.

Deliberately provider-agnostic: every rule reads from a common field shape
(`mfa_coverage.mfa_coverage_percent`, `external_sharing.anyone_with_link_count`,
etc.) rather than each provider's raw API response, so the SAME rules apply
to Google Workspace, Microsoft 365, and Dropbox Business data as long as each
provider's report code normalizes into these field names — which is why
Google's 2FA enrollment and Dropbox's member 2FA status are both surfaced as
`mfa_coverage` with the same sub-fields Microsoft's MFA coverage uses,
instead of three differently-shaped metrics needing three sets of rules.

Each rule is independent and only fires if its underlying data is actually
present — a metric that failed to fetch (network error, missing scope,
licensing wall) just means that rule silently doesn't fire, not a crash.
"""


def generate_recommendations(report_data: dict) -> list[dict]:
    """report_data: the merged dict of whatever report(s) were fetched for
    one provider (tenant report + security report, whichever ran).
    Returns a list of {severity: "high"|"medium"|"low", title, detail},
    highest severity first."""
    recs: list[dict] = []

    mfa = report_data.get("mfa_coverage")
    if mfa and mfa.get("mfa_coverage_percent") is not None:
        pct = mfa["mfa_coverage_percent"]
        if pct < 50:
            recs.append(
                {
                    "severity": "high",
                    "title": "Most users don't have MFA set up",
                    "detail": f"Only {pct}% of users ({mfa['mfa_registered']} of {mfa['total_users']}) have multi-factor authentication registered. This is the single biggest account-takeover risk reduction available.",
                }
            )
        elif pct < 100:
            recs.append(
                {
                    "severity": "medium",
                    "title": "Some users still don't have MFA set up",
                    "detail": f"{mfa['total_users'] - mfa['mfa_registered']} of {mfa['total_users']} users ({100 - pct:.1f}%) haven't registered multi-factor authentication.",
                }
            )

    security_defaults = report_data.get("security_defaults")
    if security_defaults and not security_defaults.get("enabled"):
        recs.append(
            {
                "severity": "high",
                "title": "Tenant-wide MFA enforcement (Security Defaults) is off",
                "detail": "Security Defaults requires MFA for everyone in the tenant at no extra licensing cost. It's currently disabled — anyone without MFA registered can sign in with just a password.",
            }
        )

    external_sharing = report_data.get("external_sharing")
    if external_sharing and external_sharing.get("anyone_with_link_count", 0) > 0:
        count = external_sharing["anyone_with_link_count"]
        recs.append(
            {
                "severity": "medium",
                "title": f"{count} file{'s' if count != 1 else ''} shared with \"anyone with the link\"",
                "detail": "Anyone who has the link can open these — including if it's forwarded outside your organization. Review whether each one actually needs to be public.",
            }
        )

    inactive = report_data.get("inactive_accounts")
    if inactive:
        buckets = inactive.get("days_since_last_onedrive_activity") or inactive.get("buckets") or {}
        stale = buckets.get("90+", 0)
        if stale > 0:
            recs.append(
                {
                    "severity": "low",
                    "title": f"{stale} account{'s' if stale != 1 else ''} inactive 90+ days",
                    "detail": "Consider reviewing whether these accounts (and their licenses) are still needed.",
                }
            )

    guests = report_data.get("guest_accounts")
    if guests and guests.get("total_guests", 0) > 0:
        recs.append(
            {
                "severity": "low",
                "title": f"{guests['total_guests']} external guest account{'s' if guests['total_guests'] != 1 else ''}",
                "detail": "Periodically review guest access — it's easy for a guest invited for a one-time project to keep standing access long after it's needed.",
            }
        )

    sharing_policy = report_data.get("sharing_policy")
    if sharing_policy and sharing_policy.get("sharing_capability"):
        capability = sharing_policy["sharing_capability"]
        # "Anyone"/"ExternalUserAndGuestSharing"-style values mean the
        # tenant allows sharing with people outside the organization
        # entirely, not just named guests — the most permissive setting.
        # Checked loosely (substring) since the exact enum spelling is
        # unverified against a live tenant.
        if "Anyone" in capability or "ExternalUserAndGuestSharing" in capability:
            recs.append(
                {
                    "severity": "medium",
                    "title": f"Tenant-wide external sharing is set to \"{capability}\"",
                    "detail": "This is the most permissive sharing level available — anyone can be given access to a file, not just invited guests. Worth confirming this is intentional.",
                }
            )

    app_grants = report_data.get("app_grants")
    if app_grants and app_grants.get("total_apps", 0) > 0:
        recs.append(
            {
                "severity": "low",
                "title": f"{app_grants['total_apps']} third-party app{'s' if app_grants['total_apps'] != 1 else ''} with access to your data",
                "detail": "Periodically review which apps have been granted access and what they can see — an old, forgotten app grant is a common lingering risk.",
            }
        )

    total_licenses = report_data.get("total_licenses")
    licenses_used = report_data.get("licenses_used")
    if total_licenses and licenses_used is not None and total_licenses > 0:
        utilization = licenses_used / total_licenses
        if utilization < 0.5:
            recs.append(
                {
                    "severity": "low",
                    "title": "Less than half your licenses are assigned",
                    "detail": f"{licenses_used} of {total_licenses} seats in use ({utilization * 100:.0f}%) — worth checking whether the subscription size still matches actual headcount.",
                }
            )

    growth = report_data.get("storage_growth")
    if growth and growth.get("available") and growth.get("monthly_rate_bytes", 0) > 0:
        gb_per_month = growth["monthly_rate_bytes"] / 1e9
        if gb_per_month > 10:
            recs.append(
                {
                    "severity": "low",
                    "title": f"Storage growing ~{gb_per_month:.1f} GB/month",
                    "detail": "Worth keeping an eye on if you're near a storage plan limit.",
                }
            )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: severity_order.get(r["severity"], 3))
    return recs
