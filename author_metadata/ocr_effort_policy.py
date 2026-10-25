"""Conservative regional retry limits; never a PDF failure-probability model."""


def defer_photographic_orientation_retry(task, frame, text, confidence):
    """Keep the primary reading and all other regions, including later pages.

    Only Thorough can omit an optional second segmentation of an interior
    image that looks photographic and produced essentially no alphabetic
    text. Covers, scans with native overlays, explicit bylines, initials
    repairs and technical retries keep the full schedule in both modes.
    """
    profile = frame.get("visual_profile") or {}
    photographic = profile.get("mid_tone_fraction", 0) >= .45 and profile.get("light_fraction", 1) <= .30
    exhausted_prefix = (task.get("empty_orientation_prefix_exhausted", False)
                        and profile.get("mid_tone_fraction", 0) >= .10
                        and profile.get("light_fraction", 1) <= .90)
    return (
        task.get("recognition_effort") == "thorough"
        and task.get("reason") == "image_without_native_overlay"
        and task.get("page", 0) > 1
        and sum(character.isalpha() for character in text) <= 2
        and (confidence is None or confidence < 10)
        and (photographic or exhausted_prefix)
    )


def empty_orientation_rechecks(task_results):
    """Count completed, uninformative primary/recheck pairs in a real prefix."""
    count = 0
    for task, _readings, attempts in task_results:
        if task.get("reason") != "image_without_native_overlay" or task.get("page", 0) <= 1:
            continue
        primary = [a for a in attempts if a.get("reason") == task["reason"] and a.get("status") == "read"]
        retry = [a for a in attempts if a.get("reason") == "empty_or_low_quality_orientation_recheck" and a.get("status") == "read"]
        if (primary and retry and primary[0].get("alphabetic_characters", 99) <= 2
                and retry[0].get("alphabetic_characters", 99) <= 2):
            count += 1
    return count
