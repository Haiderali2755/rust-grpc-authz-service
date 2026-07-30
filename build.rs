fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Uses the protoc binary vendored by protobuf-src via tonic-build, so a
    // fresh clone compiles without installing protoc system-wide.
    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&["proto/authz.proto"], &["proto"])?;
    Ok(())
}
