// What server/syscap does on a Mac, and the microphone too, so both are stamped on one clock. Writes
// records to stdout:
//   float64 time the first sample was captured (seconds, on QueryPerformanceCounter),
//   uint32 sample count, then float32 mono 16 kHz samples.
// The first line on stderr is "capturing", or why it can't. Follows the default device when it changes.
//
//   carnot-audio loopback        everything the computer plays
//   carnot-audio mic [NAME]      the default microphone, or the first whose name contains NAME
use std::io::Write;
use std::time::{Duration, Instant};
use windows::core::{Result, PWSTR};
use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
use windows::Win32::Media::Audio::*;
use windows::Win32::Media::Multimedia::WAVE_FORMAT_IEEE_FLOAT;
use windows::Win32::System::Com::StructuredStorage::PropVariantClear;
use windows::Win32::System::Com::*;
use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};

const RATE: u32 = 16000;
const BUFFER_100NS: i64 = 5_000_000; // half a second
const AUTOCONVERT: u32 = AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;

struct Source {
    loopback: bool,
    name: String,
}

fn enumerator() -> Result<IMMDeviceEnumerator> {
    unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
}

fn device_id(device: &IMMDevice) -> Result<String> {
    unsafe {
        let id: PWSTR = device.GetId()?;
        let text = id.to_string().unwrap_or_default();
        CoTaskMemFree(Some(id.0 as *const _));
        Ok(text)
    }
}

fn friendly_name(device: &IMMDevice) -> String {
    unsafe {
        let Ok(store) = device.OpenPropertyStore(STGM_READ) else { return String::new() };
        let Ok(mut value) = store.GetValue(&PKEY_Device_FriendlyName) else { return String::new() };
        let text = value.to_string();
        let _ = PropVariantClear(&mut value);
        text
    }
}

fn pick(source: &Source) -> Result<IMMDevice> {
    let all = enumerator()?;
    unsafe {
        if source.loopback {
            return all.GetDefaultAudioEndpoint(eRender, eConsole);
        }
        if !source.name.is_empty() {
            let want = source.name.to_lowercase();
            let list = all.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)?;
            for i in 0..list.GetCount()? {
                let d = list.Item(i)?;
                if friendly_name(&d).to_lowercase().contains(&want) {
                    return Ok(d);
                }
            }
            eprintln!("no input device matching {:?}, using the system default", source.name);
        }
        let default = all.GetDefaultAudioEndpoint(eCapture, eConsole)?;
        if !is_virtual(&friendly_name(&default)) {
            return Ok(default);
        }
        // Steam and the like make themselves the default input, and only ever deliver silence
        let list = all.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)?;
        for i in 0..list.GetCount()? {
            let d = list.Item(i)?;
            if !is_virtual(&friendly_name(&d)) {
                return Ok(d);
            }
        }
        Ok(default)
    }
}

fn is_virtual(name: &str) -> bool {
    let name = name.to_lowercase();
    ["steam streaming", "nvidia broadcast", "virtual audio", "voicemeeter", "vb-audio", "virtual cable"]
        .iter()
        .any(|v| name.contains(v))
}

fn now_seconds(frequency: i64) -> f64 {
    let mut count = 0i64;
    unsafe {
        let _ = QueryPerformanceCounter(&mut count);
    }
    count as f64 / frequency as f64
}

/// Loopback only delivers while something plays, and the echo canceller needs to know that silence
/// is silence and not a late packet. A silent stream on the same device keeps packets coming.
struct KeepAlive {
    client: IAudioClient,
    render: IAudioRenderClient,
    frames: u32,
}

impl KeepAlive {
    fn start(device: &IMMDevice) -> Result<Self> {
        unsafe {
            let client: IAudioClient = device.Activate(CLSCTX_ALL, None)?;
            let format = client.GetMixFormat()?;
            let result = client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, BUFFER_100NS, 0, format, None);
            CoTaskMemFree(Some(format as *const _));
            result?;
            let frames = client.GetBufferSize()?;
            let render: IAudioRenderClient = client.GetService()?;
            let keep = Self { client, render, frames };
            keep.fill()?;
            keep.client.Start()?;
            Ok(keep)
        }
    }

    fn fill(&self) -> Result<()> {
        unsafe {
            let free = self.frames - self.client.GetCurrentPadding()?;
            if free > 0 {
                self.render.GetBuffer(free)?;
                self.render.ReleaseBuffer(free, AUDCLNT_BUFFERFLAGS_SILENT.0 as u32)?;
            }
        }
        Ok(())
    }
}

fn wave_format() -> WAVEFORMATEX {
    WAVEFORMATEX {
        wFormatTag: WAVE_FORMAT_IEEE_FLOAT as u16,
        nChannels: 1,
        nSamplesPerSec: RATE,
        nAvgBytesPerSec: RATE * 4,
        nBlockAlign: 4,
        wBitsPerSample: 32,
        cbSize: 0,
    }
}

/// Captures until the device goes away or stops being the default. Err means start over.
fn run(source: &Source, announced: &mut bool, frequency: i64) -> Result<()> {
    let device = pick(source)?;
    let id = device_id(&device)?;
    let follows_default = source.loopback || source.name.is_empty();
    let format = wave_format();
    unsafe {
        let client: IAudioClient = device.Activate(CLSCTX_ALL, None)?;
        let flags = AUTOCONVERT | if source.loopback { AUDCLNT_STREAMFLAGS_LOOPBACK } else { 0 };
        client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_100NS, 0, &format, None)?;
        let capture: IAudioCaptureClient = client.GetService()?;
        let keep = if source.loopback { Some(KeepAlive::start(&device)?) } else { None };
        client.Start()?;
        if !*announced {
            *announced = true;
            eprintln!("capturing");
        }
        eprintln!("device: {}", friendly_name(&device));

        let mut out = std::io::stdout().lock();
        let mut record = Vec::with_capacity(8192);
        let mut checked = Instant::now();
        let mut next_time = 0.0f64;
        loop {
            std::thread::sleep(Duration::from_millis(5));
            if let Some(k) = &keep {
                k.fill()?;
            }
            while capture.GetNextPacketSize()? > 0 {
                let mut data = std::ptr::null_mut();
                let (mut frames, mut buffer_flags, mut qpc) = (0u32, 0u32, 0u64);
                capture.GetBuffer(&mut data, &mut frames, &mut buffer_flags, None, Some(&mut qpc))?;
                let bad_time = qpc == 0 || buffer_flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32 != 0;
                let time = if !bad_time {
                    qpc as f64 / 1e7
                } else if next_time > 0.0 {
                    next_time
                } else {
                    now_seconds(frequency) - frames as f64 / RATE as f64
                };
                next_time = time + frames as f64 / RATE as f64;
                record.clear();
                record.extend_from_slice(&time.to_le_bytes());
                record.extend_from_slice(&frames.to_le_bytes());
                if buffer_flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 {
                    record.resize(12 + frames as usize * 4, 0);
                } else {
                    record.extend_from_slice(std::slice::from_raw_parts(data as *const u8, frames as usize * 4));
                }
                capture.ReleaseBuffer(frames)?;
                if out.write_all(&record).and_then(|_| out.flush()).is_err() {
                    std::process::exit(0); // the service went away
                }
            }
            if follows_default && checked.elapsed() > Duration::from_secs(1) {
                checked = Instant::now();
                if pick(source).and_then(|d| device_id(&d)).map_or(true, |now| now != id) {
                    eprintln!("default device changed");
                    return Ok(());
                }
            }
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let source = match args.first().map(String::as_str) {
        Some("loopback") => Source { loopback: true, name: String::new() },
        Some("mic") => Source { loopback: false, name: args.get(1).cloned().unwrap_or_default() },
        _ => {
            eprintln!("usage: carnot-audio loopback | mic [NAME]");
            std::process::exit(2);
        }
    };
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    }
    let mut frequency = 0i64;
    unsafe {
        let _ = QueryPerformanceFrequency(&mut frequency);
    }
    let mut announced = false;
    loop {
        if let Err(e) = run(&source, &mut announced, frequency) {
            if !announced {
                eprintln!("cannot capture audio: {e}"); // the first line: the service reports it
                std::process::exit(1);
            }
            eprintln!("capture interrupted: {e}");
            std::thread::sleep(Duration::from_secs(1)); // e.g. headphones unplugged; try the new default
        }
    }
}
