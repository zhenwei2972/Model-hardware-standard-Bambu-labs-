# Let Claude run your Bambu Lab 3D printer

<!-- mcp-name: io.github.zhenwei2972/mhs-printer -->

Talk to your printer in plain words. Claude can check whether it's free, prepare a model with
**Bambu Studio** (Bambu Lab's own slicer), check the file is safe to print, start the print, watch it
through the printer's camera, and tell you afterwards how it went.

It works over your home network with no cloud account, and it asks you before anything starts.

> *"Is the printer free? Print `bracket.stl` for strength, and show me the first layer when it's down."*
>
> *"How did last night's print go?"*
>
> *"This error popped up on the printer screen. What does it mean?"*

**Printers:** Bambu Lab A1 mini, A1, P1P, P1S, X1 Carbon, X1E. It also drives Roborock Saros robot
vacuums: see [docs/ROBOROCK.md](docs/ROBOROCK.md).

## What you need

- A Bambu Lab printer on the same Wi-Fi network as your computer.
- A Mac, Windows or Linux computer with **Python 3.10 or newer**. The `python3` that comes with macOS is
  3.9, which is too old; the steps below install a newer one for you.
- **[Bambu Studio](https://bambulab.com/en/download/studio)**, installed as normal. Claude uses it to
  prepare models.
- **Claude Desktop** or **Claude Code**.

## Set it up (about 15 minutes)

**1. Let the printer accept commands from your network.** On the printer's screen go to
**Settings → WLAN**:

- Turn on **LAN Only Mode**.
- **Restart the printer.**
- Turn on **Developer Mode**. It only appears after the restart.

Note: in LAN Only Mode the Bambu Handy phone app stops working with the printer. Bambu Studio still
works, and turning LAN Only Mode off brings Handy back.

**2. Write down three things** from the same screen:

- the **IP address** (e.g. `192.168.1.42`);
- the **Access Code** (8 characters);
- the **serial number** (also on the sticker at the back; an A1 mini's starts with `030`).

**3. Install.** Open a terminal and run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh            # once; or: brew install uv
git clone https://github.com/zhenwei2972/Model-hardware-standard-Bambu-labs-.git mhs && cd mhs
uv venv --python 3.12 && uv pip install -e .
echo "$PWD/.venv/bin/mhs-mcp"                                # copy this path for the next step
```

**4. Connect it to Claude.**

- **Claude Desktop.** Open **Settings → Developer → Edit Config** and add the block below, using the path
  from step 3 and your three values. Then quit and reopen Claude Desktop.
  ```json
  {"mcpServers": {"mhs": {"command": "/the/path/from/step/3/mhs-mcp",
    "env": {"BAMBU_HOST": "192.168.1.42", "BAMBU_SERIAL": "0309CA1234567890", "BAMBU_ACCESS_CODE": "12345678"}}}}
  ```
- **Claude Code.** Run this in the `mhs` folder:
  ```bash
  claude mcp add mhs --env BAMBU_HOST=192.168.1.42 --env BAMBU_SERIAL=0309CA1234567890 \
    --env BAMBU_ACCESS_CODE=12345678 -- "$PWD/.venv/bin/mhs-mcp"
  ```

To try it without letting it print anything yet, also add `MHS_READ_ONLY=1`. Claude can then look but
not touch.

**5. Check it works.** Ask Claude: *"Is my printer connected? Is it set up to print?"* It answers with
the printer's status and anything missing, such as no filament loaded.

Stuck? [docs/SETUP.md](docs/SETUP.md) shows what each common problem looks like and how to fix it.

## How it keeps your printer safe

- **Nothing starts without a yes.** Starting, stopping and clearing errors each need a second, explicit
  confirmation.
- **Every file is checked before printing.** Models are prepared with your printer's own Bambu Studio
  profile, and the file is checked to be marked for your model. The plate temperature must suit the
  filament and the plate on your printer. A print is refused while the printer is busy or showing a
  serious error, and any problem is explained.
- **Error codes in plain words.** Printer errors are translated using Bambu's published list, and it says
  so when a code isn't on that list.
- **Your details stay at home.** The access code is kept on your computer and only ever sent to your
  printer, over an encrypted connection.

## See it in action

- **[Example print report](docs/examples/print-report-bracelet-stand.md).** A real print with a 40-minute
  filament pause, explained stage by stage.
- **[From request to print](docs/CASE_STUDY_DETOLF.md).** A display riser for an IKEA Detolf cabinet,
  designed, checked, printed and fitted.
- **[A full test run](docs/RUN_REPORT_2026-10-04_BUMPER.md).** A phone bumper, including the bug the run
  uncovered and how it was fixed.
- **[benchscale](https://github.com/zhenwei2972/benchscale)**, the companion project. It designs lab
  parts around the real objects they hold, and these tools print them.

## More detail

- [Technical reference](docs/REFERENCE.md): every tool, the architecture, the safety model in detail,
  and tests.
- [Setup troubleshooting](docs/SETUP.md) · [Watching prints](docs/MONITORING.md) ·
  [The design loop](docs/DESIGN_LOOP.md) · [Roborock vacuums](docs/ROBOROCK.md)

It is aligned with Anthropic's
[Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview):
devices are read and written through named channels, with safety limits enforced in code rather than in
the prompt. Licence: see [LICENSE](LICENSE).
