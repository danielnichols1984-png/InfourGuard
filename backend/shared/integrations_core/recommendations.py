"""A small rules engine over an individual account's own report data —
the personal-account counterpart to tenants_core.recommendations, reading
the field shapes generate_security_report()/generate_storage_report()
produce for a single Google/Dropbox/Microsoft account rather than an
entire organization. Each rule only fires if its data is actually
present, so a metric that failed to fetch just means that rule silently
doesn't fire, not a crash.
"""


def generate_recommendations(report_data: dict) -> list[dict]:
    """report_data: the merged security + storage report for one personal
    account. Returns a list of {severity: "high"|"medium"|"low", title,
    detail}, highest severity first."""
    recs: list[dict] = []

    anyone_with_link_count = report_data.get("anyone_with_link_count", 0)
    if anyone_with_link_count > 0:
        recs.append(
            {
                "severity": "medium",
                "title": f"{anyone_with_link_count} file{'s' if anyone_with_link_count != 1 else ''} shared with \"anyone with the link\"",
                "detail": "Anyone who has the link can open these, including if it's forwarded to someone else. Review whether each one still needs to be public.",
            }
        )

    mfa = report_data.get("mfa")
    if mfa:
        if mfa.get("available") and mfa.get("methods_count", 0) == 0:
            recs.append(
                {
                    "severity": "high",
                    "title": "No MFA methods registered",
                    "detail": "This account has no multi-factor authentication methods on file. Enabling MFA is the single biggest reduction in account-takeover risk available.",
                }
            )
        elif not mfa.get("available"):
            recs.append(
                {
                    "severity": "low",
                    "title": "MFA status can't be checked automatically",
                    "detail": mfa.get(
                        "note",
                        "This platform doesn't expose MFA status to third-party apps for a personal account — verify it directly in your account's security settings.",
                    ),
                }
            )

    storage_percent = report_data.get("storage_percent", 0)
    if storage_percent >= 90:
        recs.append(
            {
                "severity": "medium",
                "title": f"Storage is {storage_percent}% full",
                "detail": "Consider clearing out large or duplicate files, or upgrading your storage plan, before you run out of space.",
            }
        )

    growth = report_data.get("storage_growth")
    if growth and growth.get("available") and growth.get("monthly_rate_bytes", 0) > 0:
        gb_per_month = growth["monthly_rate_bytes"] / 1e9
        if gb_per_month > 1:
            recs.append(
                {
                    "severity": "low",
                    "title": f"Storage growing ~{gb_per_month:.1f} GB/month",
                    "detail": "Worth keeping an eye on if you're near your storage plan's limit.",
                }
            )

    shared_count = report_data.get("shared_count", 0)
    if shared_count > 10:
        recs.append(
            {
                "severity": "low",
                "title": f"{shared_count} files shared with specific people",
                "detail": "Periodically reviewing who still has access to shared files helps catch access that's outlived its purpose.",
            }
        )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: severity_order.get(r["severity"], 3))
    return recs
