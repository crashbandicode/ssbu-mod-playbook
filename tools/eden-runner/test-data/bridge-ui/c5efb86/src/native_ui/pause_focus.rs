// lines 1-25 omitted
impl PauseFocus {
    pub const UNKNOWN: Self = Self {
        open: Fact::Unknown, panel: None, row: None, index: None, tab: None, cpu_behavior: None,
    };

    pub fn write<const N: usize>(&self, out: &mut Line<N>) {
        let _ = write!(out, "pause.open={}\npause.panel=", self.open.name());
        match self.panel {
            Some(Panel::None) => out.push_bytes(b"none"),
            Some(Panel::Base) => out.push_bytes(b"base"),
            Some(Panel::Quit) => out.push_bytes(b"quit"),
            Some(Panel::Sub(n @ 2..=5)) => { let _ = write!(out, "sub{n}"); }
            Some(Panel::Other) => out.push_bytes(b"other"),
            _ => out.push_bytes(b"unknown"),
        }
        out.push_bytes(b"\npause.row=");
        write_index(out, self.row);
        out.push_bytes(b"\npause.index=");
        write_index(out, self.index);
        out.push_bytes(b"\npause.tab=");
        match self.tab {
            Some(tab @ 0..=3) => { let _ = write!(out, "{tab}"); }
            _ => out.push_bytes(b"unknown"),
        }
        let behavior = match self.cpu_behavior {
            Some(1) => "stop",
            Some(2) => "lv_n",
            Some(3) => "walk",
            Some(4) => "escape",
            Some(5) => "jump",
            Some(6) => "attack",
            Some(7) => "smash_side",
            Some(8) => "operation",
            _ => "unknown",
        };
        let _ = writeln!(out, "\npause.cpu_behavior={behavior}");
    }
}
// lines 64-64 omitted
fn write_index<const N: usize, T: core::fmt::Display>(out: &mut Line<N>, index: Option<Option<T>>) {
    match index {
        None => out.push_bytes(b"unknown"),
        Some(None) => out.push_bytes(b"none"),
        Some(Some(value)) => { let _ = write!(out, "{value}"); }
    }
}
// lines 72-168 omitted
