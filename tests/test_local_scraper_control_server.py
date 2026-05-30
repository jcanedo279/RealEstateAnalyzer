from re_analyzer.scrapers import local_scraper_control_server as server


def test_default_bind_host_is_loopback():
    args = server.parse_args([])

    assert args.host == "127.0.0.1"
    assert not server.network_bind_requires_token(args.host)


def test_network_bind_requires_token():
    assert server.network_bind_requires_token("0.0.0.0")
    assert server.network_bind_requires_token("192.168.1.10")
    assert not server.network_bind_requires_token("localhost")


def test_control_token_protects_routes():
    original_token = server.CONTROL_TOKEN
    server.CONTROL_TOKEN = "local-test-token"
    client = server.app.test_client()
    try:
        unauthorized = client.get("/api/health")
        authorized = client.get(
            "/api/health",
            headers={"Authorization": "Bearer local-test-token"},
        )

        assert unauthorized.status_code == 401
        assert authorized.status_code == 200
    finally:
        server.CONTROL_TOKEN = original_token


def test_mutating_routes_require_configured_token_by_default():
    original_token = server.CONTROL_TOKEN
    original_required = server.REQUIRE_TOKEN_FOR_MUTATIONS
    server.CONTROL_TOKEN = ""
    server.REQUIRE_TOKEN_FOR_MUTATIONS = True
    client = server.app.test_client()
    try:
        read_response = client.get("/api/health")
        write_response = client.post("/api/scraper-runs", json={})

        assert read_response.status_code == 200
        assert write_response.status_code == 503
    finally:
        server.CONTROL_TOKEN = original_token
        server.REQUIRE_TOKEN_FOR_MUTATIONS = original_required
