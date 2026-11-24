mod pause_focus;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Sample {
    observed: u64,
    at: u64,
    gen: u64,
    gap: u64,
    ui: Ui,
}

impl Sample {
    const EMPTY: Self = Self { observed: 0, at: 0, gen: 0, gap: 0, ui: Ui::UNKNOWN };

    #[cfg(test)]
    fn encode(self, rev: u64, boot: u64) -> Line<MAX_PAYLOAD> {
        self.encode_with_net(rev, boot, ldn_join::NetInfo::default())
    }

    fn current(self, now: u64) -> Self {
        if now.saturating_sub(self.at) >= EXPIRES_MS {
            Self { ui: Ui::UNKNOWN, ..self }
        } else {
            self
        }
    }

    fn encode_with_net(self, rev: u64, boot: u64, net: ldn_join::NetInfo) -> Line<MAX_PAYLOAD> {
        let mut out = Line::new();
        let pause = self.ui.pause;
        let ui = if self.ui.screen == Screen::Unknown { Ui::UNKNOWN } else { self.ui };
        let screen = ui.screen.name();
        let layer = ui.layer.unwrap_or(UNKNOWN);
        let owner = ui.owner.unwrap_or(UNKNOWN);
        let dialog = ui.dialog.unwrap_or(UNKNOWN);
        let active = ui.screen != Screen::Unknown;
        let via_tile = ui.screen.uses_tile();
        let native_owner = owner == "native"
            || (owner == dialog && matches!(dialog, "ldn.create" | "ldn.challenger" | "ldn.spread" | "ldn.wait"));
        let native_focus = active && !via_tile && native_owner;
        let focus = native_focus.then_some(ui.focus).flatten();
        // Candidate rooms may be displayed by index; only an independently qualified
        // scan record may identify the Join target.
        let room = match (ui.screen, ui.room, ui.owner, ui.dialog, focus) {
            (Screen::LdnRoomList | Screen::LdnCreate, Room::Rebuilding, _, _, _) => Room::Rebuilding,
            (Screen::LdnRoomList, Room::Ready { generation: 1.., .. }, Some("native"), Some("none"),
                Some(Focus { index: 1.., .. })) => ui.room,
            _ => Room::Unknown,
        };
        let mut id = Line::<96>::new();
        match (ui.screen, focus, room) {
            (Screen::LdnRoomList, Some(Focus { index: 0, .. }), _) => id.push_bytes(b"ldn.create_room"),
            (Screen::LdnRoomList, Some(_), Room::Ready { key, .. }) => {
                let _ = write!(id, "ldn.room.{key:08x}");
            }
            (Screen::LdnRoomList, _, _) => id.push_bytes(UNKNOWN.as_bytes()),
            (_, Some(focus), _) => id.push_bytes(focus.id.as_bytes()),
            _ => id.push_bytes(UNKNOWN.as_bytes()),
        }
        let id = core::str::from_utf8(id.as_bytes()).unwrap_or(UNKNOWN);
        let _ = write!(out, "schema=ssbu.ui/1\nrev={rev}\nboot={boot:#018x}\nack=0\nack.result=unsupported\nfocus=");
        let _ = write!(out, "{screen}");
        if active {
            if matches!(dialog, UNKNOWN | "none") {
                if ui.layer.is_some() {
                    let _ = write!(out, ".{layer}");
                }
            } else {
                let _ = write!(out, ".{dialog}");
            }
            if id != UNKNOWN {
                let _ = write!(out, ".{id}");
            }
        }
        let _ = write!(out, "\nobservation={}\nobservation.active={}\nscreen={screen}\nscreen.tab={}\nscreen.layer={layer}\nscreen.context={}\n",
            self.observed, if active { "yes" } else { "no" }, ui.tab.unwrap_or(UNKNOWN), ui.context.unwrap_or(UNKNOWN));
        let source = if via_tile { "tile.cursor" } else if focus.is_some() { "native" } else { UNKNOWN };
        let _ = write!(out, "focus.source={source}\nfocus.id={id}\n");
        number(&mut out, "focus.index", focus.map(|f| f.index));
        out.push_bytes(b"focus.label=");
        if matches!(room, Room::Ready { .. }) {
            out.push_bytes(UNKNOWN.as_bytes());
        } else if let Some(focus) = focus {
            quoted(&mut out, focus.label);
        } else {
            out.push_bytes(UNKNOWN.as_bytes());
        }
        out.push_bytes(b"\n");
        let _ = write!(out, "dialog={dialog}\ninput.owner={owner}\n");
        if !ui.screen.uses_tile() {
            let _ = writeln!(out, "input.ready={}", input_ready(&ui).name());
        }
        let _ = write!(out, "screen.gen={}\nscreen.gap={}\n", self.gen, self.gap);
        match ui.picker {
            Some((phase, age, latch)) => { let _ = writeln!(out, "picker.phase={phase}.{age}.{latch}"); }
            None => out.push_bytes(b"picker.phase=unknown\n"),
        }
        let _ = writeln!(out, "ldn.role={}", ui.role.unwrap_or(UNKNOWN));
        out.push_bytes(b"ldn.members=unknown\nldn.self.slot=unknown\n");
        let _ = writeln!(out, "ldn.request_pending={}", ui.request_pending.name());
        number(&mut out, "ldn.request.slot", ui.request_slot);
        let prompt = match ui.prompt {
            Some("challenger") => "challenger",
            Some("spread") => "spread",
            _ => UNKNOWN,
        };
        let _ = writeln!(out, "ldn.prompt={prompt}");
        out.push_bytes(b"ldn.prompt.ready=unknown\nldn.prompt.focus=unknown\n");
        match room {
            Room::Unknown | Room::Rebuilding => {
                let ready = if room == Room::Rebuilding { "no" } else { UNKNOWN };
                let _ = write!(out, "ldn.room.key=unknown\nldn.room.rows=unknown\nldn.room.key.count=unknown\nldn.scan.generation=unknown\nldn.room.ready={ready}\n");
            }
            Room::Ready { generation, key, rows, matches } => {
                let _ = write!(out, "ldn.room.key={key:08x}\nldn.room.rows={rows}\nldn.room.key.count={matches}\nldn.scan.generation={generation}\nldn.room.ready=yes\n");
            }
        }
        out.push_bytes(b"ldn.net.key=");
        if let Some(key) = net.key { let _ = write!(out, "{key:08x}"); } else { out.push_bytes(b"unknown"); }
        out.push_bytes(b"\nldn.net.nodes=");
        if let Some((count, max)) = net.nodes { let _ = write!(out, "{count}/{max}"); } else { out.push_bytes(b"unknown"); }
        out.push_bytes(b"\nldn.net.bssid=");
        if let Some(bssid) = net.bssid { hex(&mut out, &bssid); } else { out.push_bytes(b"unknown"); }
        out.push_bytes(b"\nldn.net.session=");
        if let Some(session) = net.session { hex(&mut out, &session); } else { out.push_bytes(b"unknown"); }
        out.push_bytes(b"\n");
        pause.write(&mut out);
        out
    }
}

fn number<const N: usize>(out: &mut Line<N>, key: &str, number: Option<impl core::fmt::Display>) {
    if let Some(number) = number {
        let _ = writeln!(out, "{key}={number}");
    } else {
        let _ = writeln!(out, "{key}={UNKNOWN}");
    }
}
#[cfg(test)]
mod tests;
#[cfg(test)]
mod gate_tests;
