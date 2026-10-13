-- This file was protected using Luraph Obfuscator v15.0 [https://lura.ph/]
-- Synthetic register VM authored for Fusion regression tests.
local function make(env, proto, upvalues)
    local ops = proto[1]
    local constants = proto[2]
    local closure
    closure = function(...)
        local pc = 1
        local registers = {...}
        while true do
            local op = ops[pc]
            if op == 1 then
                registers[1] = constants[1]
            elseif op == 2 then
                registers[2] = constants[2]
            elseif op == 3 then
                registers[3] = registers[1] + registers[2]
            elseif op == 4 then
                print(registers[3])
            else
                return registers[3]
            end
            pc = pc + 1
        end
    end
    return closure
end
local main = make({print = print}, {{1, 2, 3, 4, 5}, {20, 22}}, {})
main()
