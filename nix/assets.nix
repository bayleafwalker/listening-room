{
  lib,
  stdenvNoCC,
  python,
  ffmpeg,
}:
stdenvNoCC.mkDerivation {
  pname = "listening-room-assets";
  version = "1";
  # Only authored asset inputs invalidate the expensive render. UI, provider
  # and metadata changes reuse these exact reviewed scenes and sound beds.
  src = lib.cleanSourceWith {
    src = ../src;
    filter =
      path: type:
      let
        relative = lib.removePrefix (toString ../src + "/") (toString path);
      in
      (
        type == "directory"
        && (
          toString path == toString ../src
          || builtins.elem relative [
            "artwork"
            "scene_tuning"
          ]
          || lib.hasPrefix "artwork/" relative
          || lib.hasPrefix "scene_tuning/" relative
        )
      )
      || lib.hasPrefix "artwork/" relative
      || lib.hasPrefix "scene_tuning/" relative
      || builtins.elem relative [
        "render_vistas.py"
        "render_atmospheres.py"
        "motion_engine.py"
        "visual_scenes.py"
        "atmosphere_catalogue.py"
        "validate_vistas.py"
      ];
  };
  nativeBuildInputs = [
    python
    ffmpeg
  ];
  buildPhase = ''
    runHook preBuild
    python3 render_vistas.py assets
    python3 render_atmospheres.py assets
    python3 validate_vistas.py assets
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -r assets/. "$out/"
    runHook postInstall
  '';
  dontFixup = true;
}
