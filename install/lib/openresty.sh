#!/usr/bin/env bash

# Conditional OpenResty and pinned Lua dependency installation.

LUA_RESTY_HMAC_REPO="https://github.com/jkeys089/lua-resty-hmac.git"
LUA_RESTY_HMAC_RELEASE="0.06-1"
LUA_RESTY_HMAC_REVISION="23da759b69f208576526c8ac21b7c5ad66740321"

LUA_RESTY_JWT_REPO="https://github.com/SkyLothar/lua-resty-jwt.git"
LUA_RESTY_JWT_RELEASE="v0.1.11"
LUA_RESTY_JWT_REVISION="ee1d024071f872e2b5a66eaaf9aeaf86c5bab3ed"

LUA_RESTY_AUTO_SSL_VERSION="0.13.1-1"
OPENRESTY_JWT_SECRET_PATH="/etc/seismic/openresty-jwt-secret"

install_pinned_openresty_lua_library() {
    local description=$1
    local repository=$2
    local release=$3
    local revision=$4
    local staging
    local lua_files

    staging=$(mktemp -d)
    info "Fetching $description $release..."

    if ! git -C "$staging" init -q \
        || ! git -C "$staging" remote add origin "$repository" \
        || ! git -C "$staging" fetch -q --depth 1 origin "$revision" \
        || ! git -C "$staging" checkout -q --detach FETCH_HEAD; then
        rm -rf "$staging"
        die "Could not fetch pinned $description revision $revision"
    fi

    mapfile -t lua_files < <(find "$staging/lib/resty" -maxdepth 1 -type f -name '*.lua' -print)
    if ((${#lua_files[@]} == 0)); then
        rm -rf "$staging"
        die "Pinned $description release contains no lib/resty/*.lua files"
    fi

    install -d -o root -g root -m 755 /usr/local/openresty/lualib/resty
    if ! install -o root -g root -m 644 "${lua_files[@]}" \
        /usr/local/openresty/lualib/resty/; then
        rm -rf "$staging"
        die "Could not install $description Lua files"
    fi

    rm -rf "$staging"
    success "$description $release installed at revision $revision"
}

install_openresty() {
    local runtime_masked=false

    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" != true ]]; then
        info "Public endpoint disabled; skipping OpenResty installation."
        return
    fi

    section "Installing OpenResty"

    if command -v openresty >/dev/null 2>&1; then
        info "OpenResty is already installed; keeping the existing package."
    else
        info "Adding the official OpenResty package repository..."
        if ! curl -fsSL https://openresty.org/package/pubkey.gpg \
            | gpg --dearmor --yes -o /usr/share/keyrings/openresty.gpg; then
            die "Could not install the OpenResty package signing key."
        fi

        printf '%s\n' \
            "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/openresty.gpg] https://openresty.org/package/ubuntu $(lsb_release -sc) main" \
            > /etc/apt/sources.list.d/openresty.list

        if ! apt-get update >>"$LOG_FILE" 2>&1; then
            die "apt-get update failed after adding the OpenResty repository; see $LOG_FILE"
        fi
        command -v systemctl >/dev/null 2>&1 \
            || die "systemctl is required to install OpenResty without auto-starting it."
        [[ -d /run/systemd/system ]] \
            || die "systemd is not running; refusing to install OpenResty because service auto-start cannot be controlled."

        info "Temporarily masking OpenResty to prevent package auto-start..."
        systemctl mask --runtime openresty.service >>"$LOG_FILE" 2>&1 \
            || die "Could not create the temporary OpenResty runtime mask."
        runtime_masked=true

        if ! apt-get install -y -- openresty >>"$LOG_FILE" 2>&1; then
            if [[ "$runtime_masked" == true ]]; then
                systemctl unmask --runtime openresty.service >>"$LOG_FILE" 2>&1 || true
            fi
            die "OpenResty package installation failed; see $LOG_FILE"
        fi

        if [[ "$runtime_masked" == true ]]; then
            systemctl unmask --runtime openresty.service >>"$LOG_FILE" 2>&1 \
                || die "OpenResty installed but its temporary runtime mask could not be removed."
        fi
        systemctl disable --now openresty.service >>"$LOG_FILE" 2>&1 \
            || die "Could not leave the newly installed OpenResty service disabled and stopped."
        success "OpenResty package installed without leaving the service enabled or running"
    fi

    info "Installing lua-resty-auto-ssl $LUA_RESTY_AUTO_SSL_VERSION..."
    if ! luarocks show lua-resty-auto-ssl "$LUA_RESTY_AUTO_SSL_VERSION" \
        >/dev/null 2>&1; then
        if ! luarocks install lua-resty-auto-ssl "$LUA_RESTY_AUTO_SSL_VERSION" \
            >>"$LOG_FILE" 2>&1; then
            die "lua-resty-auto-ssl installation failed; see $LOG_FILE"
        fi
    fi
    success "lua-resty-auto-ssl $LUA_RESTY_AUTO_SSL_VERSION installed"

    install_pinned_openresty_lua_library \
        "lua-resty-hmac" \
        "$LUA_RESTY_HMAC_REPO" \
        "$LUA_RESTY_HMAC_RELEASE" \
        "$LUA_RESTY_HMAC_REVISION"
    install_pinned_openresty_lua_library \
        "lua-resty-jwt" \
        "$LUA_RESTY_JWT_REPO" \
        "$LUA_RESTY_JWT_RELEASE" \
        "$LUA_RESTY_JWT_REVISION"

    if [[ ! -f /etc/ssl/resty-auto-ssl-fallback.crt \
        || ! -f /etc/ssl/resty-auto-ssl-fallback.key ]]; then
        info "Generating the OpenResty fallback TLS certificate..."
        if ! openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
            -subj '/CN=sni-support-required-for-valid-ssl' \
            -keyout /etc/ssl/resty-auto-ssl-fallback.key \
            -out /etc/ssl/resty-auto-ssl-fallback.crt \
            >>"$LOG_FILE" 2>&1; then
            die "Could not generate the OpenResty fallback TLS certificate."
        fi
        chmod 600 /etc/ssl/resty-auto-ssl-fallback.key
        chmod 644 /etc/ssl/resty-auto-ssl-fallback.crt
    fi

    install -d -o nobody -g nogroup -m 700 /etc/resty-auto-ssl
    install -d -o root -g root -m 755 /usr/local/openresty/nginx/lua
    success "OpenResty dependencies are ready; configuration has not been deployed or started."
}

setup_openresty_jwt_secret() {
    local legacy_lua="/usr/local/openresty/nginx/lua/jwt_auth.lua"
    local legacy_secret=""
    local staging

    install -d -o root -g root -m 0755 /etc/seismic

    if [[ -e "$OPENRESTY_JWT_SECRET_PATH" || -L "$OPENRESTY_JWT_SECRET_PATH" ]]; then
        [[ ! -L "$OPENRESTY_JWT_SECRET_PATH" ]] \
            || die "OpenResty JWT secret must not be a symbolic link: $OPENRESTY_JWT_SECRET_PATH"
        [[ -s "$OPENRESTY_JWT_SECRET_PATH" ]] \
            || die "OpenResty JWT secret exists but is empty: $OPENRESTY_JWT_SECRET_PATH"
        chown root:nogroup "$OPENRESTY_JWT_SECRET_PATH"
        chmod 0640 "$OPENRESTY_JWT_SECRET_PATH"
        info "Reusing the existing OpenResty JWT secret."
        return
    fi

    if [[ -f "$legacy_lua" && ! -L "$legacy_lua" ]]; then
        legacy_secret=$(sed -n 's/.*local JWT_SECRET = "\([^"]*\)".*/\1/p' "$legacy_lua" | head -n 1)
    fi

    staging=$(mktemp /etc/seismic/.openresty-jwt-secret.XXXXXX)
    chmod 0600 "$staging"
    if [[ -n "$legacy_secret" ]]; then
        printf '%s\n' "$legacy_secret" >"$staging"
        info "Migrating the existing embedded OpenResty JWT secret."
    elif ! openssl rand -base64 32 >"$staging"; then
        rm -f -- "$staging"
        die "Could not generate the OpenResty JWT secret."
    else
        info "Generated a new OpenResty JWT secret."
    fi

    [[ -s "$staging" ]] || {
        rm -f -- "$staging"
        die "Generated OpenResty JWT secret is empty."
    }
    chown root:nogroup "$staging"
    chmod 0640 "$staging"
    mv -- "$staging" "$OPENRESTY_JWT_SECRET_PATH"
    success "OpenResty JWT secret stored securely; its value was not logged."
}

validate_openresty_templates() {
    local template_root="$TEMPLATES_DIR/openresty"
    local required=(
        "$template_root/nginx.conf"
        "$template_root/logrotate-openresty"
        "$template_root/lua/jwt_auth.lua"
        "$template_root/lua/rate_limit.lua"
    )
    local path

    for path in "${required[@]}"; do
        [[ -f "$path" ]] || die "Required OpenResty template not found: $path"
    done
}

deploy_openresty_configuration() {
    local template_root="$TEMPLATES_DIR/openresty"
    local staging

    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" != true ]]; then
        info "Public endpoint disabled; skipping OpenResty configuration deployment."
        return
    fi

    section "Deploying OpenResty configuration"
    command -v openresty >/dev/null 2>&1 \
        || die "OpenResty is not installed."
    validate_openresty_templates

    staging=$(mktemp -d)
    if ! sed "s|DOMAIN_NAME_PLACEHOLDER|$DOMAIN|g" \
        "$template_root/nginx.conf" >"$staging/nginx.conf" \
        || ! sed \
            -e "s|RATE_LIMIT_RPS_PLACEHOLDER|$RATE_LIMIT_RPS|g" \
            -e "s|RATE_LIMIT_BURST_PLACEHOLDER|$RATE_LIMIT_BURST|g" \
            "$template_root/lua/rate_limit.lua" >"$staging/rate_limit.lua" \
        || ! cp "$template_root/lua/jwt_auth.lua" "$staging/jwt_auth.lua"; then
        rm -rf -- "$staging"
        die "Could not render the OpenResty configuration templates."
    fi

    if grep -R -n '_PLACEHOLDER' "$staging" >>"$LOG_FILE" 2>&1; then
        rm -rf -- "$staging"
        die "Rendered OpenResty configuration still contains placeholders; see $LOG_FILE"
    fi

    setup_openresty_jwt_secret

    info "Testing the staged OpenResty configuration..."
    if ! openresty -t \
        -p /usr/local/openresty/nginx/ \
        -c "$staging/nginx.conf" >>"$LOG_FILE" 2>&1; then
        rm -rf -- "$staging"
        die "Staged OpenResty configuration validation failed; see $LOG_FILE"
    fi

    install -d -o root -g root -m 0755 /usr/local/openresty/nginx/lua
    install -o root -g root -m 0644 \
        "$staging/jwt_auth.lua" \
        /usr/local/openresty/nginx/lua/jwt_auth.lua
    install -o root -g root -m 0644 \
        "$staging/rate_limit.lua" \
        /usr/local/openresty/nginx/lua/rate_limit.lua
    install -o root -g root -m 0644 \
        "$staging/nginx.conf" \
        /usr/local/openresty/nginx/conf/nginx.conf
    install -o root -g root -m 0644 \
        "$template_root/logrotate-openresty" \
        /etc/logrotate.d/openresty
    rm -rf -- "$staging"

    if ! openresty -t >>"$LOG_FILE" 2>&1; then
        die "Installed OpenResty configuration validation failed; see $LOG_FILE"
    fi

    success "OpenResty configuration deployed for https://$DOMAIN."
    info "OpenResty was not started, enabled, or reloaded."
}
