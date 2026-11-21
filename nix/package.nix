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
  # The rendered scene loops and sound beds, built from src/artwork and
  # src/scene_tuning only (see assets.nix).
  assets = import ./assets.nix {
    inherit lib stdenvNoCC ffmpeg;
    python = assetPython;
  };
in
stdenvNoCC.mkDerivation {
  pname = "listening-hub";
  version = "2.3.2";
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../src
      ../tests
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
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good
    gst_all_1.gst-plugins-bad
    gst_all_1.gst-libav
  ];

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
    python3 tests/test_listening_hub.py
    python3 tests/test_listening_ambience.py
    python3 tests/test_listening_rooms.py
    python3 tests/test_listening_visuals.py
    python3 tests/test_listening_scene_tuning.py
    python3 tests/test_listening_effects.py
    python3 tests/test_listening_twitch.py
    python3 tests/test_listening_layers.py
    python3 tests/test_listening_browse.py
    python3 tests/test_listening_credentials.py
    python3 tests/test_listening_atmospheres.py
    python3 tests/test_listening_atmosphere_character.py
    python3 tests/test_listening_devices.py
    python3 tests/test_listening_session.py
    python3 tests/test_listening_session_push.py
    python3 tests/test_listening_online.py
    python3 tests/test_listening_experience.py
    python3 tests/test_listening_picture_races.py
    python3 tests/test_listening_desktop_stations.py
    python3 tests/test_listening_stations.py
    python3 tests/test_listening_metadata_store.py
    python3 tests/test_listening_metadata_pipeline.py
    python3 tests/test_listening_metadata_http.py
    python3 tests/test_listening_metadata_cli.py
    python3 tests/test_listening_provider_auth.py
    python3 tests/test_listening_room_volume.py
    python3 tests/test_listening_controller_cost.py
    python3 tests/test_listening_next_model.py
    python3 tests/test_listening_next_glance.py
    python3 tests/test_listening_render_cache.py
    python3 tests/test_listening_next_rooms.py
    python3 tests/test_listening_rooms_oracles.py
    python3 tests/test_listening_glance_oracles.py
    python3 tests/test_listening_listen.py
    python3 tests/test_listening_listen_oracles.py
    python3 tests/test_listening_next_prefs.py
    python3 tests/test_listening_overlay_swap.py
    python3 tests/test_listening_sources.py
    python3 tests/test_listening_o6_oracles.py
    python3 tests/test_listening_o9_ux.py
    # The sandbox has no /etc/dbus-1, so the private bus gets its config explicitly; the test sees
    # it is already isolated (LISTENING_PROBE_ISOLATED) and does not re-execute itself.
    smoke_runtime=$(mktemp -d)
    chmod 700 "$smoke_runtime"
    XDG_RUNTIME_DIR="$smoke_runtime" GDK_BACKEND=x11 GDK_DEBUG=no-portals GSK_RENDERER=cairo \
      LISTENING_PROBE_ISOLATED=1 LISTENING_HUB_ASSETS="$PWD/assets" \
      xvfb-run -a -s '-screen 0 1440x1000x24' \
      dbus-run-session --config-file=${dbus}/share/dbus-1/session.conf \
      python3 tests/test_listening_window_smoke.py
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
    makeWrapper ${python}/bin/python3 "$out/bin/listening-hub" \
      --add-flags "$out/share/listening-hub/listening_hub.py" \
      --set LISTENING_HUB_CATALOG "$out/share/listening-hub/catalogue.json" \
      --set LISTENING_HUB_ASSETS "$out/share/listening-hub/assets" \
      --set LISTENING_VOICE_MODEL "$out/share/listening-hub/models/voice-instrumental.onnx" \
      --set LISTENING_EMBED_MODEL "$out/share/listening-hub/models/msd-musicnn.onnx" \
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
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
        ]
      }"
    makeWrapper ${python}/bin/python3 "$out/bin/listening-room" \
      --add-flags "$out/share/listening-hub/listening_room.py" \
      --set LISTENING_HUB_CATALOG "$out/share/listening-hub/catalogue.json" \
      --set LISTENING_HUB_ASSETS "$out/share/listening-hub/assets" \
      --set LISTENING_VOICE_MODEL "$out/share/listening-hub/models/voice-instrumental.onnx" \
      --set LISTENING_EMBED_MODEL "$out/share/listening-hub/models/msd-musicnn.onnx" \
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
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
        ]
      }"
    for entry in listening-session:session_service.py listening-roomctl:roomctl.py listening-session-client:session_client.py; do
      makeWrapper ${python}/bin/python3 "$out/bin/''${entry%%:*}" \
        --add-flags "$out/share/listening-hub/''${entry#*:}" \
        --set LISTENING_HUB_CATALOG "$out/share/listening-hub/catalogue.json" \
        --set LISTENING_HUB_ASSETS "$out/share/listening-hub/assets" \
      --set LISTENING_VOICE_MODEL "$out/share/listening-hub/models/voice-instrumental.onnx" \
      --set LISTENING_EMBED_MODEL "$out/share/listening-hub/models/msd-musicnn.onnx" \
        --prefix PATH : "$out/bin:${
          lib.makeBinPath [
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
          ]
        }"
    done
    makeWrapper ${feishin}/bin/feishin "$out/bin/listening-hub-jellyfin" \
      --set SERVER_TYPE jellyfin \
      ${lib.optionalString (jellyfinUrl != "") ''
        --set SERVER_NAME ${lib.escapeShellArg jellyfinName} \
        --set SERVER_URL ${lib.escapeShellArg jellyfinUrl} \
      ''}--unset SERVER_LOCK
    runHook postInstall
  '';

  # Lets scripts/listening-dev.sh reuse the reviewed scene render without
  # rebuilding or re-testing the whole package.
  passthru = { inherit assets; };

  meta = {
    description = "Native listening menu for desktop players, internet radio and music discovery";
    mainProgram = "listening-hub";
    platforms = lib.platforms.linux;
    license = lib.licenses.gpl3Plus;
  };
}
