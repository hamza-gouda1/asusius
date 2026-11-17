# A 15-minute hardware report

Use this checklist on an ASUS laptop with Asusius 0.3.0 already installed from
[the release](https://github.com/hamza-gouda1/asusius/releases/tag/v0.3.0).
Installation time is separate. Run the commands as your desktop user in a terminal.
Close the simulated demo and open the installed app for these checks. Confirm the
version in **About** is 0.3.0. In the collection terminal,
`echo "${ASUSIUS_FAKE_ROOT:-}"` should print a blank line. If it prints a path, open
a terminal outside the demo environment before collecting readings.

## Minutes 0–3 — save the baseline

The following block creates a private report directory and saves read-only output.
Keep this terminal open so the later commands use the same directory. An unavailable
service or unsupported feature is useful evidence. Its error and exit code are saved.

```sh
report_dir=$(mktemp -d "$HOME/asusius-hardware-XXXXXX")
chmod 700 "$report_dir"
export report_dir
{
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    uname -srmo
    cat /etc/os-release
    for field in sys_vendor product_name bios_version; do
        printf '\n%s\n' "$field"
        cat "/sys/class/dmi/id/$field"
    done
    printf '\nSession %s; desktop %s\n' "$XDG_SESSION_TYPE" "$XDG_CURRENT_DESKTOP"
    gnome-shell --version
    python3 -c 'from asusius import __version__; print(__version__)'
} > "$report_dir/environment.txt" 2>&1
for command in capabilities status limits; do
    asusius "$command" --json > "$report_dir/$command.json" 2> "$report_dir/$command.stderr"
    printf '%s exit=%s\n' "$command" "$?" >> "$report_dir/command-results.txt"
done
printf 'Report directory %s\n' "$report_dir"
```

The installed launcher may keep its Python package outside the system interpreter's
search path. If the version command fails, copy the version from **About** into
`environment.txt`.

## Minutes 3–9 — inspect the real pages

Record each result as **works**, **unavailable**, **fails**, or **untested** in
`observations.md` inside the report directory. Include the symptom and steps for any
failure, plus whether the laptop is on AC or battery. Use an unavailable value as
reported and keep measured zero values distinct.

| Page | Check |
| --- | --- |
| Dashboard | Cards populate and remain responsive for 30 seconds. |
| Processor and Fans | CPU utilization changes with ordinary activity. Temperatures and fan readings have sensible units. |
| Memory | Usage and Breakdown histories populate. Hover and pin a sample. Open App memory and check that Asusius appears. Compare usable RAM with `free -h`. |
| Processes | Expand Apps, find Asusius, and open its Details. Close Details and collapse the sections. |
| Graphics | Check the currently available GPU readings and any sleeping/unavailable message. |
| Battery | Check level, charging state, health and charge limit. Open History. A new machine may have little history. Note any attached peripherals. |
| Storage | Scan a small test folder you choose. Switch Treemap/Rings and navigate one level down and back. |
| About and Performance | Check capability explanations, especially unavailable firmware power limits. |

Save `free -h` output with `free -h > "$report_dir/free.txt"`. Capture a screenshot
of a failure after hiding unrelated windows and choosing a folder without private
filenames. The baseline checklist checks readings and navigation. Mark write controls
untested unless you complete the optional reversible checks below.

## Minutes 9–12 — optional reversible controls

Record the starting setting before each check, make one change, verify it, then restore
and verify the original. Stop at an error and report it. Keep the laptop powered and
save your work first.

- If profiles are available, switch Balanced/Quiet and restore the starting profile.
  Observe the selected card and the Quick Settings indicator if its extension is installed.
- If keyboard brightness is available, change one step and restore it.
- If refresh-rate choices are available, choose another supported rate and use Undo.
- Visit Touchpad & gestures, change pages, and verify that pointer speed and existing
  gesture bindings stay stable. Record any spontaneous change.

Leave GPU-mode switches, fan-curve edits, power-limit writes, charge-limit changes,
service actions, process signals and package/firmware installations for separate testing.
This report should identify the controls that were actually exercised.

## Minutes 12–15 — record the outcome and save it

Save another status snapshot after restoring settings.

```sh
asusius status --json > "$report_dir/status-after.json" 2> "$report_dir/status-after.stderr"
printf 'status-after exit=%s\n' "$?" >> "$report_dir/command-results.txt"
```

Add the following to `observations.md`.

- Laptop model, distribution, kernel, desktop version and session type.
- Asusius version and installation method.
- Checks completed, unavailable features, failures, and controls restored.
- Screenshots and the steps needed to reproduce any problem.
- Whether another hardware tool was active, such as asusctl or G-Helper through a VM.

Review the files before sharing. Remove usernames, serials, MAC/IP addresses and private
file paths from output and screenshots. Keep the laptop model, firmware version and
capability error messages that help diagnose compatibility.

The directory is the saved report. Send it privately to the maintainer, or copy its
reviewed contents into the repository's **Hardware report** issue form when you choose.
The checklist creates local files. Publication is a separate action for the tester.
