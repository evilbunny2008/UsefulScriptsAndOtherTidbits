// Overrides the hard-coded 10 minute run time of Tuya TS0601_water_switch valves
// when started with the physical button.
//
// The firmware ignores countdown changes during a button-started run, but honours
// them when the valve is switched off and back on. So when the valve reports a
// start that was NOT requested over MQTT (= physical button), this restarts it with
// the desired countdown. The valve's own countdown still turns it off, so nothing
// is left running if Zigbee2MQTT goes down.
//
// Only values reported by the device itself are trusted, restarts run one at a time
// per device, and each step waits for the device to confirm it, since the valve
// answers with a delay of several seconds and drops commands that overlap. The
// countdown and the ON are sent in a single Tuya frame (like the Tuya hub does) so
// the valve can't apply one without the other.

// Friendly name (or IEEE address) -> minutes per valve endpoint (1..1440)
const DURATIONS = {
    Tap_01: {l1: 60, l2: 60},
    Tap_02: {l1: 60, l2: 60},
};

// Tuya datapoints per valve endpoint
const STATE_DP = {l1: 1, l2: 2};
const COUNTDOWN_DP = {l1: 13, l2: 14};
const DP_TYPE_BOOL = 1;
const DP_TYPE_VALUE = 2;

// A start within this window after an MQTT "ON" for the same valve is treated as remote
const REMOTE_WINDOW_MS = 30000;
// How long to wait for the device to confirm a state change before retrying
const CONFIRM_TIMEOUT_MS = 15000;
// The device reports its countdown at least once a minute while running
const COUNTDOWN_TIMEOUT_MS = 75000;
const RETRIES = 3;
// Pause between the confirmed OFF and the ON so the valve registers a fresh start
const OFF_ON_GAP_MS = 2000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export default class TuyaValveButtonDuration {
    constructor(zigbee, mqtt, state, publishEntityState, eventBus, enableDisableExtension, restartCallback, addExtension, settings, logger) {
        this.zigbee = zigbee;
        this.state = state;
        this.eventBus = eventBus;
        this.settings = settings;
        this.logger = logger;
        // "<device>/<endpoint>" -> timestamp of last MQTT command switching it on
        this.lastRemoteOn = new Map();
        // "<device>/<endpoint>" -> last on/off state reported by the device
        this.reported = new Map();
        // "<device>/<endpoint>/<state|countdown>" -> {match, resolve} while waiting for a report
        this.waiters = new Map();
        // device name -> promise chain, so restarts on one device never overlap
        this.queues = new Map();
        this.seq = 0;
    }

    async start() {
        this.eventBus.onMQTTMessage(this, (data) => this.onMQTTMessage(data));
        this.eventBus.onDeviceMessage(this, (data) => this.onDeviceMessage(data));
    }

    async stop() {
        this.eventBus.removeListeners(this);
    }

    getConfig(entity) {
        return DURATIONS[entity.name] ?? DURATIONS[entity.ieeeAddr];
    }

    onMQTTMessage(data) {
        const baseTopic = this.settings.get().mqtt.base_topic;
        const match = data.topic.match(new RegExp(`^${baseTopic}/(.+?)(?:/(l\\d))?/set$`));
        if (!match) return;

        const entity = this.zigbee.resolveEntity(match[1]);
        if (!entity || !this.getConfig(entity)) return;

        let message;
        try {
            message = JSON.parse(data.message);
        } catch {
            message = {state: data.message};
        }

        for (const [key, value] of Object.entries(message)) {
            const endpoint = key === "state" ? match[2] : key.match(/^state_(l\d)$/)?.[1];
            if (endpoint && `${value}`.toUpperCase() !== "OFF") {
                this.lastRemoteOn.set(`${entity.name}/${endpoint}`, Date.now());
            }
        }
    }

    onDeviceMessage({device, cluster, data}) {
        const config = this.getConfig(device);
        if (!config || cluster !== "manuSpecificTuya" || !Array.isArray(data?.dpValues)) return;

        for (const [endpoint, minutes] of Object.entries(config)) {
            const key = `${device.name}/${endpoint}`;

            const countdownDp = data.dpValues.find((dp) => dp.dp === COUNTDOWN_DP[endpoint]);
            if (countdownDp) this.resolveWaiter(`${key}/countdown`, countdownDp.data.readUInt32BE(0));

            const stateDp = data.dpValues.find((dp) => dp.dp === STATE_DP[endpoint]);
            if (!stateDp) continue;

            const state = stateDp.data[0] === 1 ? "ON" : "OFF";
            // Fall back to the cached state so a report after a Zigbee2MQTT restart is not seen as a start
            const previous = this.reported.get(key) ?? this.state.get(device)[`state_${endpoint}`];
            this.reported.set(key, state);

            const waiting = this.waiters.has(`${key}/state`);
            this.resolveWaiter(`${key}/state`, state);

            if (state === "ON" && previous !== "ON" && !waiting && Date.now() - (this.lastRemoteOn.get(key) ?? 0) > REMOTE_WINDOW_MS) {
                this.logger.info(`ValveButtonDuration: button start on '${device.name}' ${endpoint}, restarting with ${minutes} min`);
                this.enqueue(device, () => this.restart(device, endpoint, minutes));
            }
        }
    }

    resolveWaiter(key, value) {
        const waiter = this.waiters.get(key);
        if (waiter?.match(value)) {
            this.waiters.delete(key);
            waiter.resolve(value);
        }
    }

    waitFor(key, match, timeout) {
        return new Promise((resolve) => {
            this.waiters.set(key, {match, resolve});
            setTimeout(() => {
                if (this.waiters.get(key)?.resolve === resolve) this.waiters.delete(key);
                resolve(undefined);
            }, timeout);
        });
    }

    enqueue(device, task) {
        const queue = (this.queues.get(device.name) ?? Promise.resolve()).then(task).catch((error) => {
            this.logger.error(`ValveButtonDuration: restart on '${device.name}' failed: ${error.message}`);
        });
        this.queues.set(device.name, queue);
    }

    async restart(device, endpoint, minutes) {
        const key = `${device.name}/${endpoint}`;

        for (let attempt = 1; attempt <= RETRIES; attempt++) {
            if (this.reported.get(key) !== "ON") return; // switched off in the meantime

            await this.sendConfirmed(device, endpoint, [this.stateDp(endpoint, false)], "OFF");
            await sleep(OFF_ON_GAP_MS);

            // Any countdown report from here on tells us which countdown the new run got
            const countdown = this.waitFor(`${key}/countdown`, () => true, CONFIRM_TIMEOUT_MS + COUNTDOWN_TIMEOUT_MS);
            await this.sendConfirmed(device, endpoint, [this.countdownDp(endpoint, minutes), this.stateDp(endpoint, true)], "ON");

            const reported = await countdown;
            if (reported === undefined) {
                this.logger.warning(`ValveButtonDuration: '${device.name}' ${endpoint} is on but did not report its countdown`);
                return;
            }
            if (reported > 10 || minutes <= 10) {
                this.logger.info(`ValveButtonDuration: '${device.name}' ${endpoint} running, device reports ${reported} min left`);
                return;
            }
            this.logger.warning(`ValveButtonDuration: '${device.name}' ${endpoint} reports ${reported} min, not ${minutes} (attempt ${attempt}/${RETRIES})`);
        }
        throw new Error(`'${device.name}' ${endpoint} kept the default countdown`);
    }

    async sendConfirmed(device, endpoint, dpValues, expected) {
        const key = `${device.name}/${endpoint}`;
        for (let attempt = 1; attempt <= RETRIES; attempt++) {
            if (this.reported.get(key) === expected) return;

            const confirmed = this.waitFor(`${key}/state`, (state) => state === expected, CONFIRM_TIMEOUT_MS);
            if (expected === "ON") this.lastRemoteOn.set(key, Date.now());
            await this.send(device, dpValues);

            if (await confirmed) return;
            this.logger.warning(`ValveButtonDuration: '${device.name}' ${endpoint} did not confirm ${expected} (attempt ${attempt}/${RETRIES})`);
        }
        throw new Error(`'${device.name}' ${endpoint} never confirmed ${expected}`);
    }

    stateDp(endpoint, on) {
        return {dp: STATE_DP[endpoint], datatype: DP_TYPE_BOOL, data: Buffer.from([on ? 1 : 0])};
    }

    countdownDp(endpoint, minutes) {
        const data = Buffer.alloc(4);
        data.writeUInt32BE(minutes);
        return {dp: COUNTDOWN_DP[endpoint], datatype: DP_TYPE_VALUE, data};
    }

    async send(device, dpValues) {
        this.seq = (this.seq + 1) % 0xffff;
        try {
            await device.zh.getEndpoint(1).command("manuSpecificTuya", "dataRequest", {seq: this.seq, dpValues}, {disableDefaultResponse: true});
        } catch (error) {
            // Failed sends are retried by sendConfirmed when no confirmation arrives
            this.logger.warning(`ValveButtonDuration: sending to '${device.name}' failed: ${error.message}`);
        }
    }
}
