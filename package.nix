{ lib
, stdenvNoCC
, makeWrapper
, shellcheck
, bash
, coreutils
, findutils
, gnugrep
, gnused
, python3
, util-linux
}:

stdenvNoCC.mkDerivation {
  pname = "typhoon-hil";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./bin ./lib ./libexec ./image ./share ];
  };

  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ shellcheck ];
  buildInputs = [ bash ];

  dontConfigure = true;
  dontBuild = true;

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    shellcheck --external-sources --severity=warning \
      bin/typhoon-hil lib/contract.sh libexec/*.sh image/container-entrypoint.sh
    runHook postCheck
  '';

  # image/ and the Python helpers run INSIDE the container, where no store path
  # exists. The automatic shebang rewrite would point them at a bash that is not
  # there, so only the host-side scripts are patched, by hand, below.
  dontPatchShebangs = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/typhoon-hil
    cp -r lib libexec image $out/share/typhoon-hil/
    install -Dm755 bin/typhoon-hil $out/bin/typhoon-hil
    install -Dm644 share/typhoon-hil-control-center.desktop \
      $out/share/applications/typhoon-hil-control-center.desktop

    patchShebangs --host $out/bin/typhoon-hil $out/share/typhoon-hil/libexec/*.sh

    # podman is deliberately NOT on this PATH. Rootless podman depends on the
    # host's setuid newuidmap and its containers.conf, so the host's own podman
    # is the only one that works.
    wrapProgram $out/bin/typhoon-hil \
      --set-default TYPHOON_HIL_ROOT $out/share/typhoon-hil \
      --prefix PATH : ${lib.makeBinPath [ coreutils findutils gnugrep gnused python3 util-linux ]}

    runHook postInstall
  '';

  meta = {
    description = "Typhoon HIL Control Center and Virtual HIL from a container: image recipe, workstation launcher and CI entry point";
    longDescription = ''
      Contains none of Typhoon HIL's software. The operator supplies the
      vendor's installer to build the image, and their own licenses to run it.
    '';
    license = lib.licenses.asl20;
    mainProgram = "typhoon-hil";
    # Control Center's Linux build is x86_64 only.
    platforms = [ "x86_64-linux" ];
  };
}
