// lines 1-12 omitted
mod pause_focus;
// lines 14-512 omitted
    fn name(self) -> &'static str {
        match self {
            Self::Unknown => UNKNOWN,
            Self::Title => "title",
            Self::Attract => "attract",
            Self::MainMenu => "main_menu",
            Self::CssTraining => "css.training",
            Self::CssSmash => "css.smash",
            Self::StageSelect => "stage_select",
            Self::Training => "training",
            Self::Rules => "rules",
            Self::Results => "results",
            Self::Pause => "pause",
            Self::LdnRoomList => "ldn.room_list",
            Self::LdnCreate => "ldn.create",
            Self::LdnWaiting => "ldn.waiting",
        }
    }
// lines 531-762 omitted
fn ui_stream_line(ui: &Ui, gen: u64, gap: u64) -> Line<{ crate::abi::SLOT_BYTES }> {
    let mut out = Line::new();
    let _ = write!(out, "[Bridge] ui screen={} ctx={} ready={} gen={} gap={} owner={} dialog={} picker=",
        ui.screen.name(), ui.context.unwrap_or(UNKNOWN), input_ready(ui).name(), gen, gap,
        ui.owner.unwrap_or(UNKNOWN), ui.dialog.unwrap_or(UNKNOWN));
    match ui.picker {
        Some((phase, age, latch)) => { let _ = write!(out, "{phase}.{age}.{latch}"); }
        None => out.push_bytes(UNKNOWN.as_bytes()),
    }
    out.push_bytes(b" raw=");
    match ui.picker_raw {
        Some((depth, [s0, s1, s2], current, age, latch)) => {
            let _ = write!(out, "{depth}:{s0},{s1},{s2}:{current}:{age}:{latch}");
        }
        None => out.push_bytes(UNKNOWN.as_bytes()),
    }
    out
}
// lines 781-884 omitted
    fn encode(self, rev: u64, boot: u64) -> Line<MAX_PAYLOAD> {
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
            || (owner == dialog && matches!(dialog, "ldn.create" | "ldn.challenger" | "ldn.spread"));
        let native_focus = active && !via_tile && native_owner;
        let focus = native_focus.then_some(ui.focus).flatten();
        // Candidate rooms may be displayed by index; only an independently qualified
        // scan record may identify the Join target.
        let room = match (ui.screen, ui.room, ui.owner, ui.dialog, focus) {
            (Screen::LdnRoomList | Screen::LdnCreate, Room::Rebuilding, _, _, _) => Room::Rebuilding,
            (Screen::LdnRoomList, Room::Ready { .. }, Some("native"), Some("none"), Some(Focus { index: 1.., .. })) => ui.room,
            _ => Room::Unknown,
        };
        let mut id = Line::<96>::new();
        match (ui.screen, focus, room) {
            (Screen::LdnRoomList, Some(Focus { index: 0, .. }), _) => id.push_bytes(b"ldn.create_room"),
            (Screen::LdnRoomList, Some(_), Room::Ready { bssid, session, .. }) => {
                id.push_bytes(b"ldn.room.");
                hex(&mut id, &bssid);
                id.push_bytes(b".");
                hex(&mut id, &session);
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
        if let Some(focus) = focus {
            quoted(&mut out, focus.label);
        } else {
            out.push_bytes(UNKNOWN.as_bytes());
        }
        out.push_bytes(b"\n");
        let _ = write!(out, "dialog={dialog}\ninput.owner={owner}\ninput.ready={}\nscreen.gen={}\nscreen.gap={}\n",
            input_ready(&ui).name(), self.gen, self.gap);
        match ui.picker {
            Some((phase, age, latch)) => { let _ = writeln!(out, "picker.phase={phase}.{age}.{latch}"); }
            None => out.push_bytes(b"picker.phase=unknown\n"),
        }
        let _ = writeln!(out, "ldn.role={}", ui.role.unwrap_or(UNKNOWN));
        number(&mut out, "ldn.members", ui.members);
        let _ = write!(out, "ldn.request_pending={}\nldn.prompt={}\n", ui.request_pending.name(), ui.prompt.unwrap_or(UNKNOWN));
        match room {
            Room::Unknown | Room::Rebuilding => {
                let ready = if room == Room::Rebuilding { "no" } else { UNKNOWN };
                let _ = write!(out, "ldn.scan.generation=unknown\nldn.room.bssid=unknown\nldn.room.session=unknown\nldn.room.ready={ready}\n");
            }
            Room::Ready { generation, bssid, session } => {
                let _ = write!(out, "ldn.scan.generation={generation}\nldn.room.bssid=");
                hex(&mut out, &bssid);
                out.push_bytes(b"\nldn.room.session=");
                hex(&mut out, &session);
                out.push_bytes(b"\nldn.room.ready=yes\n");
            }
        }
        pause.write(&mut out);
        out
    }
// lines 971-972 omitted
fn number<const N: usize>(out: &mut Line<N>, key: &str, number: Option<impl core::fmt::Display>) {
    if let Some(number) = number {
        let _ = writeln!(out, "{key}={number}");
    } else {
        let _ = writeln!(out, "{key}={UNKNOWN}");
    }
}
// lines 980-1039 omitted
pub(crate) fn is_transport_only_snapshot(snapshot: &[u8]) -> bool {
    let lines = snapshot.split(|&byte| byte == b'\n');
    [b"menu.admission=unknown".as_slice(), b"input.owner=unknown", b"focus=unknown"]
        .iter()
        .all(|required| lines.clone().any(|line| line == *required))
}
// lines 1046-1241 omitted
