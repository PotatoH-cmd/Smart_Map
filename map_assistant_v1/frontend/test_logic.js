// frontend/test_logic.js

function getStatus(depth, control) {
    // Replicating logic from MapComponent.jsx
    // Logic: Measured > Control -> Above (Green); <= Control -> Below (Red)
    if (depth !== undefined && control !== undefined) {
        const isAbove = Number(depth) > Number(control);
        const color = isAbove ? "green" : "red";
        const text = isAbove ? "高于控制高程" : "低于控制高程";
        return { isAbove, color, text };
    }
    return null;
}

console.log("Running Unit Tests for Seed Mineable Area Logic...");
console.log("Logic: Measured > Control -> Green (Above); Measured <= Control -> Red (Below)\n");

// Test Cases
const testCases = [
    { depth: 21.127, control: 19.700, expected: { color: "green", text: "高于控制高程" }, desc: "Case 1: Measured > Control (21.127 > 19.700)" },
    { depth: "21.127", control: "19.700", expected: { color: "green", text: "高于控制高程" }, desc: "Case 2: Measured > Control (String Input)" },
    { depth: 19.700, control: 19.700, expected: { color: "red", text: "低于控制高程" }, desc: "Case 3: Measured == Control (Boundary Value)" },
    { depth: 15.0, control: 19.700, expected: { color: "red", text: "低于控制高程" }, desc: "Case 4: Measured < Control (15.0 < 19.700)" },
];

let passed = 0;
testCases.forEach(tc => {
    const result = getStatus(tc.depth, tc.control);
    const passColor = result.color === tc.expected.color;
    const passText = result.text === tc.expected.text;
    
    if (passColor && passText) {
        console.log(`[PASS] ${tc.desc}`);
    } else {
        console.error(`[FAIL] ${tc.desc}`);
        console.error(`       Expected: Color=${tc.expected.color}, Text=${tc.expected.text}`);
        console.error(`       Got:      Color=${result.color}, Text=${result.text}`);
    }
    if (passColor && passText) passed++;
});

console.log(`\nTest Result: ${passed}/${testCases.length} passed.`);

if (passed === testCases.length) {
    process.exit(0);
} else {
    process.exit(1);
}
