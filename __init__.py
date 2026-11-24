import bpy
from bpy.props import EnumProperty, FloatProperty

GROUP_NAME = "CB_Simulator"

IDENTITY = ((1, 0, 0), (0, 1, 0), (0, 0, 1))

# Rows = output channel (R, G, B), columns = input (R, G, B). Linear RGB.
# Dichromacy matrices: Machado et al. 2009, severity 1.0.
MATRICES = {
    "PROTAN": ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    "DEUTAN": ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
    "TRITAN": ((1.255528, -0.076749, -0.178779),
               (-0.078411, 0.930809, 0.147602),
               (0.004733, 0.691367, 0.303900)),
    # Rec.709 luminance (linear) for a black & white / value check
    "MONO": ((0.2126, 0.7152, 0.0722),) * 3,
}

MODES = [
    ("OFF", "Off", "Disable simulation"),
    ("PROTAN", "Protanopia", "Red-blind"),
    ("DEUTAN", "Deuteranopia", "Green-blind"),
    ("TRITAN", "Tritanopia", "Blue-blind"),
    ("MONO", "Black & White", "Luminance only"),
]


def blended_matrix(mode, severity):
    m = MATRICES[mode]
    return tuple(
        tuple((1 - severity) * IDENTITY[r][c] + severity * m[r][c] for c in range(3))
        for r in range(3)
    )


# ---------------------------------------------------------------- compositor
def get_comp_tree(scene, create=False):
    # Blender 5.0+: scene.compositing_node_group
    if hasattr(scene, "compositing_node_group"):
        tree = scene.compositing_node_group
        if tree is None and create:
            tree = bpy.data.node_groups.new("Compositing", "CompositorNodeTree")
            tree.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
            rl = tree.nodes.new("CompositorNodeRLayers")
            out = tree.nodes.new("NodeGroupOutput")
            out.location = (400, 0)
            tree.links.new(rl.outputs[0], out.inputs[0])
            scene.compositing_node_group = tree
        return tree
    # Blender 4.x
    if create and not scene.use_nodes:
        scene.use_nodes = True
    return scene.node_tree


def find_output(tree):
    outs = [n for n in tree.nodes if n.bl_idname in {"CompositorNodeComposite", "NodeGroupOutput"}]
    outs.sort(key=lambda n: n.bl_idname != "CompositorNodeComposite")
    return outs[0] if outs else None


def new_math(nodes):
    for idname in ("CompositorNodeMath", "ShaderNodeMath"):
        try:
            return nodes.new(idname)
        except RuntimeError:
            continue
    raise RuntimeError("No Math node available")


def get_group():
    g = bpy.data.node_groups.get(GROUP_NAME)
    if g is None:
        g = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
        g.interface.new_socket("Image", in_out='INPUT', socket_type='NodeSocketColor')
        g.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
    return g


def build_group(g, matrix):
    g.nodes.clear()
    n_in = g.nodes.new("NodeGroupInput")
    n_in.location = (-700, 0)
    n_out = g.nodes.new("NodeGroupOutput")
    n_out.location = (1000, 0)

    sep = g.nodes.new("CompositorNodeSeparateColor")
    sep.location = (-450, 0)
    comb = g.nodes.new("CompositorNodeCombineColor")
    comb.location = (800, 0)
    for n in (sep, comb):
        if hasattr(n, "mode"):
            n.mode = 'RGB'

    g.links.new(n_in.outputs[0], sep.inputs[0])
    g.links.new(sep.outputs[3], comb.inputs[3])          # alpha passthrough
    g.links.new(comb.outputs[0], n_out.inputs[0])

    for row in range(3):
        y = 250 - row * 300
        muls = []
        for col in range(3):
            m = new_math(g.nodes)
            m.operation = 'MULTIPLY'
            m.inputs[1].default_value = matrix[row][col]
            m.location = (-200, y - col * 80)
            g.links.new(sep.outputs[col], m.inputs[0])
            muls.append(m)
        a1 = new_math(g.nodes)
        a1.operation = 'ADD'
        a1.location = (150, y)
        g.links.new(muls[0].outputs[0], a1.inputs[0])
        g.links.new(muls[1].outputs[0], a1.inputs[1])
        a2 = new_math(g.nodes)
        a2.operation = 'ADD'
        a2.location = (450, y)
        g.links.new(a1.outputs[0], a2.inputs[0])
        g.links.new(muls[2].outputs[0], a2.inputs[1])
        g.links.new(a2.outputs[0], comb.inputs[row])


OUTPUT_TYPES = {"CompositorNodeComposite", "NodeGroupOutput", "CompositorNodeViewer"}


def sim_nodes(tree):
    return [n for n in tree.nodes if n.get("cbsim")]


def install(tree):
    group = get_group()
    for target in [n for n in tree.nodes if n.bl_idname in OUTPUT_TYPES]:
        sock = target.inputs[0]
        if not sock.links:
            continue
        link = sock.links[0]
        if link.from_node.get("cbsim"):
            continue  # already filtered
        src = link.from_socket
        node = tree.nodes.new("CompositorNodeGroup")
        node["cbsim"] = True
        node.label = "Color Vision Sim"
        node.node_tree = group
        node.location = (target.location.x - 250, target.location.y)
        tree.links.remove(link)
        tree.links.new(src, node.inputs[0])
        tree.links.new(node.outputs[0], sock)


def uninstall(tree):
    for node in sim_nodes(tree):
        src = node.inputs[0].links[0].from_socket if node.inputs[0].links else None
        targets = [l.to_socket for l in node.outputs[0].links]
        tree.nodes.remove(node)
        if src:
            for t in targets:
                tree.links.new(src, t)


def view3d_spaces(context):
    win = context.window
    if win is None:
        return
    for area in win.screen.areas:
        if area.type == 'VIEW3D':
            for s in area.spaces:
                if s.type == 'VIEW3D':
                    yield s


def apply(context):
    scene = context.scene
    mode = scene.cbsim_mode
    spaces = list(view3d_spaces(context))

    if mode == 'OFF':
        tree = get_comp_tree(scene)
        if tree:
            uninstall(tree)
        if scene.get("cbsim_set_compositor"):
            for s in spaces:
                s.shading.use_compositor = 'DISABLED'
            scene["cbsim_set_compositor"] = False
    else:
        tree = get_comp_tree(scene, create=True)
        build_group(get_group(), blended_matrix(mode, scene.cbsim_severity))
        install(tree)
        for s in spaces:
            if s.shading.use_compositor != 'ALWAYS':
                s.shading.use_compositor = 'ALWAYS'
                scene["cbsim_set_compositor"] = True

    if context.window:
        for area in context.window.screen.areas:
            area.tag_redraw()


# ---------------------------------------------------------------- UI
class CBSIM_OT_set(bpy.types.Operator):
    bl_idname = "cbsim.set"
    bl_label = "Set Color Vision Mode"
    bl_options = {'REGISTER'}

    mode: EnumProperty(items=MODES)

    def execute(self, context):
        context.scene.cbsim_mode = self.mode
        apply(context)
        return {'FINISHED'}


def draw_panel(layout, scene):
    col = layout.column(align=True)
    for ident, name, _ in MODES:
        op = col.operator("cbsim.set", text=name, depress=(scene.cbsim_mode == ident))
        op.mode = ident
    layout.separator()
    layout.prop(scene, "cbsim_severity", slider=True)


class CBSIM_PT_panel(bpy.types.Panel):
    bl_label = "Color Vision"
    bl_idname = "CBSIM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Vision"

    def draw(self, context):
        draw_panel(self.layout, context.scene)


class CBSIM_PT_panel_node(bpy.types.Panel):
    bl_label = "Color Vision"
    bl_idname = "CBSIM_PT_panel_node"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Color Vision"

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == 'CompositorNodeTree'

    def draw(self, context):
        draw_panel(self.layout, context.scene)


def severity_update(self, context):
    if self.cbsim_mode != 'OFF':
        apply(context)


classes = (CBSIM_OT_set, CBSIM_PT_panel, CBSIM_PT_panel_node)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.cbsim_mode = EnumProperty(items=MODES, default="OFF")
    bpy.types.Scene.cbsim_severity = FloatProperty(
        name="Severity", min=0.0, max=1.0, default=1.0, update=severity_update)


def unregister():
    del bpy.types.Scene.cbsim_severity
    del bpy.types.Scene.cbsim_mode
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
