
local host=getfenv(0);local hs,hp,pr=host.setfenv,host.pcall,host.print
local safe={};for _,k in ipairs({"assert","error","ipairs","pairs","next","select","tonumber","tostring","type","typeof","unpack","rawequal","rawget","rawset","setmetatable","getmetatable","bit32","coroutine","math","string","table","utf8"})do safe[k]=host[k]end
safe._G=safe;safe.getfenv=function()return safe end;safe.setfenv=function(f,e)if e~=safe then error("blocked env",0)end return hs(f,safe)end
safe.print=function()end;safe.warn=function()end;local stop="__MVV2_HTTP_STOP__";local paths={};local events={};local requests={};local captured={};local proxyMeta={}
local function summarize(v,depth,seen)
 if type(v)=="string"then local z={};for i=1,#v do local b=string.byte(v,i);if b>=32 and b<=126 and b~=34 and b~=92 then z[#z+1]=string.char(b)else z[#z+1]=string.format("\\x%02X",b)end end;return "\""..table.concat(z).."\"" end
 if type(v)=="number"or type(v)=="boolean"or v==nil then return tostring(v)end
 if type(v)=="function"then return "<function>"end
 if paths[v]then return "<"..paths[v]..">"end
 if type(v)~="table"then return "<"..type(v)..">"end
 if depth>=3 then return "<table>"end;seen=seen or {};if seen[v]then return "<cycle>"end;seen[v]=true
 local p={};for k,x in pairs(v)do if #p>=16 then p[#p+1]="…";break end;p[#p+1]=summarize(k,depth+1,seen).."="..summarize(x,depth+1,seen)end;return "{"..table.concat(p,",").."}"
end
local function event(kind,path,args)
 if #events>=500 then return end;local p={};for i=1,math.min(#args,8)do p[i]=summarize(args[i],0,{})end;events[#events+1]={kind,path,table.concat(p," | ")}
end
local makeProxy
local function network(kind,args)
 local v=args[1];if type(v)=="table"then v=v.Url or v.url or v.URL or v end;local u=type(v)=="string"and v or tostring(v);requests[#requests+1]={kind,u};event("HTTP",kind,{u})
 if #requests==1 and (kind=="HttpGet"or kind=="GetAsync")then return ""end;error(stop,0)
end
proxyMeta.__index=function(self,k)
 local path=paths[self].."."..tostring(k)
 return makeProxy(path)
end
proxyMeta.__call=function(self,...)
 local path=paths[self];local args={...};if paths[args[1]]then table.remove(args,1)end
 local method=path:match("%.([^%.]+)$")or path
 if method=="HttpGet"or method=="HttpGetAsync"or method=="HttpPost"or method=="GetAsync"or method=="PostAsync"or method=="RequestAsync"then return network(method,args)end
 event("CALL",path,args);local method=path:match("%.([^%.]+)$")or path;if method=="Toggle"or method=="Slider"then for _,a in ipairs(args)do if type(a)=="table"and type(a.Callback)=="function"then captured[method]=a.Callback end end end;return makeProxy(path.."()")
end
proxyMeta.__newindex=function(t,k,v)event("SET",paths[t].."."..tostring(k),{v});rawset(t,k,v)end
proxyMeta.__tostring=function(self)return "<proxy:"..(paths[self]or "?")..">"end
proxyMeta.__len=function()return 0 end
makeProxy=function(path)local p={};paths[p]=path;return setmetatable(p,proxyMeta)end
local envproxy=makeProxy("game");safe.game=envproxy;safe.Game=envproxy;safe.workspace=makeProxy("workspace");safe.Instance=makeProxy("Instance");safe.Enum=makeProxy("Enum");safe.Color3=makeProxy("Color3");safe.Vector2=makeProxy("Vector2");safe.Vector3=makeProxy("Vector3");safe.UDim2=makeProxy("UDim2");safe.TweenInfo=makeProxy("TweenInfo");safe.task=makeProxy("task");safe.shared=makeProxy("shared");safe.script=makeProxy("script")
safe.wait=function()end;safe.spawn=function()return makeProxy("task.spawn-result")end;safe.delay=safe.spawn;safe.getgenv=function()return safe end;safe.getrenv=function()return safe end
safe.loadstring=function(code)event("LOADSTRING","loadstring",{type(code)=="string" and ("<source length "..#code..">")or type(code)});return function()return makeProxy("WindUI")end end
safe.require=function(name)event("REQUIRE","require",{name});return makeProxy("require-result")end
safe.HttpGet=function(self,...)local a={...};return network("global HttpGet",a)end
safe.pcall=function(fn,...)local ok,a,b,c,d=hp(fn,...);if not ok and a==stop then error(stop,0)end;return ok,a,b,c,d end
local function run()
 local o=__TABLE__;local n=__NAMES__;local function cap(...)return function()end end
 for _,v in pairs(o)do if type(v)=="function"then hp(hs,v,safe)end end;for k,v in pairs(o)do if v==host then o[k]=safe end end
 o.__capture=cap;local ok=hp(function()return o[__REMAPPER__](o,n,"__capture")end);if not ok then pr("@@FAIL|remap");return end
 local qi,ui=o.q,o.u
 o.q=function(self,...)local f=qi(self,...);if type(f)~="function"then return f end;return function(x,...)local y=f(x,...);if type(y)=="string"then event("DECODE-q","q",{x,y})end;return y end end
 o.u=function(self,...)local f=ui(self,...);if type(f)~="function"then return f end;return function(x,...)local y=f(x,...);if type(y)=="string"then event("DECODE-u","u",{x,y})end;return y end end
 local good,err=hp(function()return o[__ENTRY__](o)end);pr("@@EXEC",good and "returned"or(err==stop and "aborted-network"or"stopped"));if good then if captured.Toggle then local ok1,e1=hp(captured.Toggle,true);event("SIM","Toggle(true)",{ok1 and "ok"or tostring(e1)})end;if captured.Slider then local ok2,e2=hp(captured.Slider,80);event("SIM","Slider(80)",{ok2 and "ok"or tostring(e2)})end;if captured.Toggle then local ok3,e3=hp(captured.Toggle,false);event("SIM","Toggle(false)",{ok3 and "ok"or tostring(e3)})end end
 for i,r in ipairs(requests)do pr("@@REQ|"..i.."|"..r[1].."|"..r[2])end
 for i,e in ipairs(events)do pr("@@EV|"..i.."|"..e[1].."|"..e[2].."|"..e[3])end
end
hs(run,safe);local ok,err=hp(run);if not ok then pr("@@HARNESS|error")end
