{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  mpv,
  playerctl,
  pulseaudio,
  rofi,
  libnotify,
  xdg-utils,
  systemd,
  flatpak,
  feishin,
  gtk4,
  libadwaita,
  adwaita-icon-theme,
  gst_all_1,
  gobject-introspection,
  wrapGAppsHook4,
  ffmpeg,
  chromaprint,
  wireplumber,
  hyprland,
  yt-dlp,
  xvfb-run,
  dbus,
  # Optional Jellyfin address for the packaged Feishin launcher. Empty leaves
  # the server for the user to add in the app.
  jellyfinUrl ? "",
  jellyfinName ? "Jellyfin music",
  # Optional MTG model weights (CC BY-NC-SA 4.0), see nix/models.nix.
  voiceModel,
  embeddingModel,
}:
let
  assetPython = python3.withPackages (ps: [
    ps.pygobject3
    ps.pycairo
    ps.pillow
  ]);
  python = python3.withPackages (ps: [
    ps.pygobject3
    ps.pycairo
    ps.pillow
    ps.numpy
    ps.onnxruntime
    ps.librosa
    ps.soundfile
  ]);
  # Programs the launchers may start; every entry point gets the same PATH.
  runtimePath = lib.makeBinPath [
    mpv
    playerctl
    pulseaudio
    rofi
    libnotify
    xdg-utils
    systemd
    flatpak
    feishin
    wireplumber
    hyprland
    yt-dlp
    ffmpeg
    chromaprint
  ];
  # One launcher for a Python entry point in $out/share/listening-hub.
  wrapPython = name: script: ''
    makeWrapper ${python}/bin/python3 "$out/bin/${name}" \
      --add-flags "$out/share/listening-hub/${script}" \
      --set LISTENING_HUB_CATALOG "$out/share/listening-hub/catalogue.json" \
      --set LISTENING_HUB_ASSETS "$out/share/listening-hub/assets" \
      --set LISTENING_VOICE_MODEL "$out/share/listening-hub/models/voice-instrumental.onnx" \
      --set LISTENING_EMBED_MODEL "$out/share/listening-hub/models/msd-musicnn.onnx" \
      --prefix PATH : "$out/bin:${runtimePath}"
  '';
  # GStreamer for the scene loops. The dev shell takes this same list (passthru), so the
  # dev loop and acceptance play loops exactly as the package does.
  gstPlugins = [
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good
    gst_all_1.gst-plugins-bad
    gst_all_1.gst-libav
  ];
  # The rendered scene loops and sound beds, built from src/artwork and
  # src/scene_tuning only (see assets.nix).
  rendered = import ./assets.nix {
    inherit lib stdenvNoCC ffmpeg;
    python = assetPython;
  };
  # The render plus every poster pre-scaled to card size (NAME.card.jpg), so opening the
  # window never decodes a full poster for a card. Kept apart from the render so changing
  # the card size does not re-render the scenes.
  assets = stdenvNoCC.mkDerivation {
    pname = "listening-room-scene-assets";
    version = "1";
    dontUnpack = true;
    nativeBuildInputs = [ assetPython ];
    buildPhase = ''
      runHook preBuild
      cp -r --no-preserve=mode ${rendered} assets
      python3 ${../src/poster_cards.py} assets
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      cp -r assets "$out"
      runHook postInstall
    '';
    dontFixup = true;
  };
in
stdenvNoCC.mkDerivation {
  pname = "listening-hub";
  version = "2.7.0";
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../src
      ../tests
      ../CHANGELOG.md   # the CHANGELOG oracles (o11h C1/C2) read it
    ];
  };
  nativeBuildInputs = [
    makeWrapper
    python
    gobject-introspection
    wrapGAppsHook4
    ffmpeg
  ];
  # libadwaita draws the next window. GStreamer lets it play a room's reviewed
  # scene loop (muted); without it the window falls back to still art.
  buildInputs = [
    gtk4
    libadwaita
    adwaita-icon-theme
  ]
  ++ gstPlugins;

  buildPhase = ''
    runHook preBuild
    ln -s ${assets} assets
    runHook postBuild
  '';

  doCheck = true;
  # The window smoke test builds the real window on a private Xvfb with its own D-Bus.
  nativeCheckInputs = [
    xvfb-run
    dbus
  ];
  checkPhase = ''
    runHook preCheck
    export LISTENING_HUB_SOURCE="$PWD/src/listening_hub.py"
    export LISTENING_HUB_CATALOG="$PWD/src/catalogue.json"
    export LISTENING_AMBIENCE_SOURCE="$PWD/src/ambience.py"
    export PYTHONPATH="$PWD/src:$PWD/tests"
    export PYTHONDONTWRITEBYTECODE=1
    # Every tests/test_*.py runs unless tests/suites-manual.txt names it; the window suites and
    # station EOF below need their own environment and run after.
    python3 tests/run_suites.py --except test_listening_window_smoke.py test_listening_o10_control.py test_listening_station_eof.py
    # The sandbox has no /etc/dbus-1, so the private bus gets its config explicitly; the test sees
    # it is already isolated (LISTENING_PROBE_ISOLATED) and does not re-execute itself.
    for window_suite in test_listening_window_smoke.py test_listening_o10_control.py; do
      smoke_runtime=$(mktemp -d)
      chmod 700 "$smoke_runtime"
      XDG_RUNTIME_DIR="$smoke_runtime" GDK_BACKEND=x11 GDK_DEBUG=no-portals GSK_RENDERER=cairo \
        LISTENING_PROBE_ISOLATED=1 LISTENING_HUB_ASSETS="$PWD/assets" \
        xvfb-run -a -s '-screen 0 1440x1000x24' \
        dbus-run-session --config-file=${dbus}/share/dbus-1/session.conf \
        python3 tests/$window_suite
    done
    LISTENING_TEST_MPV=${mpv}/bin/mpv python3 tests/test_listening_station_eof.py
    python3 src/validate_vistas.py assets
    python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw"
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/listening-hub" "$out/bin"
    cp src/*.py src/catalogue.json "$out/share/listening-hub/"
    cp -r assets "$out/share/listening-hub/"
    cp -r src/scene_tuning "$out/share/listening-hub/"
    mkdir -p "$out/share/listening-hub/models"
    ln -s ${voiceModel} "$out/share/listening-hub/models/voice-instrumental.onnx"
    ln -s ${embeddingModel} "$out/share/listening-hub/models/msd-musicnn.onnx"
    cp src/models/README.md "$out/share/listening-hub/models/README.md"
    ${wrapPython "listening-hub" "listening_hub.py"}
    ${wrapPython "listening-room" "listening_room.py"}
    ${wrapPython "listening-session" "session_service.py"}
    ${wrapPython "listening-roomctl" "roomctl.py"}
    ${wrapPython "listening-session-client" "session_client.py"}
    makeWrapper ${feishin}/bin/feishin "$out/bin/listening-hub-jellyfin" \
      --set SERVER_TYPE jellyfin \
      ${lib.optionalString (jellyfinUrl != "") ''
        --set SERVER_NAME ${lib.escapeShellArg jellyfinName} \
        --set SERVER_URL ${lib.escapeShellArg jellyfinUrl} \
      ''}--unset SERVER_LOCK
    runHook postInstall
  '';

  # Lets scripts/listening-dev.sh reuse the reviewed scene render without
  # rebuilding or re-testing the whole package, and the dev shell reuse the
  # package's Python environment and GStreamer plugins.
  passthru = { inherit assets python gstPlugins; };

  meta = {
    description = "Native listening menu for desktop players, internet radio and music discovery";
    mainProgram = "listening-hub";
    platforms = lib.platforms.linux;
    license = lib.licenses.gpl3Plus;
  };
}
