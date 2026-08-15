# -*- coding: utf-8 -*-
"""
模型工具 (Model Tools) - Blender Add-on
一键清理隐藏顶点组权重 + 固定参数 FBX 快速导出。
兼容 Blender 4.x / 5.x。
"""

bl_info = {
    "name": "模型工具",
    "author": "Ducc",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-Panel > 模型工具",
    "description": "清理隐藏顶点组权重 + 固定参数 FBX 快速导出",
    "category": "Object",
}

import bpy


# -----------------------------------------------------------------------------
# 工具函数：统计真实三角面数
# -----------------------------------------------------------------------------
def get_triangle_count(mesh):
    """返回 mesh 的真实三角面数量（三角化后的 Loop Triangles 数量）。

    不用 len(mesh.polygons)，因为一个四边面 Polygon 实际渲染/导出时是 2 个三角面，
    Polygon 数量会低估真实三角面数。
    """
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)


# -----------------------------------------------------------------------------
# Operator: 模型减面 (Decimate Modifier - Collapse)
# -----------------------------------------------------------------------------
class MODELTOOLS_OT_decimate(bpy.types.Operator):
    """对当前选中 Mesh 应用 Decimate Modifier (COLLAPSE 模式) 进行减面。

    实现方式：
    - 用 mesh.loop_triangles 统计真实三角面数（而不是 polygons 数量）。
    - 按 目标三角面数 / 当前三角面数 计算 Decimate.ratio，Blender 官方文档确认
      COLLAPSE 模式下 ratio 就是按三角面比例计算的（"Ratio of triangles to
      reduce to (collapse only)"），所以这个比例可以直接使用，不需要额外换算。
    - decimate_type 固定为 'COLLAPSE'（边坍缩），保持整体形状/轮廓，比直接删除
      顶点或用 UNSUBDIV/DISSOLVE 更适合保留 UV、材质分区。
    - use_collapse_triangulate=True，确保减面后网格已经是三角化的，这样减面前后
      的三角面统计口径一致，也方便后续实际检查是否接近目标值。
    - Decimate Modifier 默认不影响 UV、材质、Vertex Groups 数据本身；vertex_group
      参数可以让减面强度按顶点组权重分布（本功能未使用该细粒度控制，全网格统一减面）。
    - 减面完成后调用 modifier_apply 把 Modifier 固化到网格数据上。
    """
    bl_idname = "modeltools.decimate"
    bl_label = "开始减面"
    bl_description = "使用 Decimate Modifier (COLLAPSE) 将模型减面到接近目标三角面数"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        obj = context.active_object

        if obj is None or obj.type != "MESH":
            self.report({"WARNING"}, "请先选中一个 Mesh 对象")
            return {"CANCELLED"}

        scene = context.scene
        target_triangles = scene.modeltools_target_triangles

        prev_mode = obj.mode
        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        current_triangles = get_triangle_count(obj.data)

        if target_triangles >= current_triangles:
            self.report({"WARNING"}, "目标面数高于当前模型，无需减面。")
            scene.modeltools_current_triangles = current_triangles
            return {"CANCELLED"}

        if target_triangles < 100:
            self.report({"WARNING"}, "目标面数过低，可能严重影响模型质量。")

        # 是否保留原模型：开启则先复制一份，对复制体执行减面，保留原模型不变
        target_obj = obj
        if scene.modeltools_keep_original:
            new_mesh_data = obj.data.copy()
            new_obj = obj.copy()
            new_obj.data = new_mesh_data
            new_obj.name = obj.name + "_low"
            context.collection.objects.link(new_obj)
            target_obj = new_obj

        # 根据 当前三角面数 / 目标三角面数 计算初始 Decimate Ratio
        ratio = target_triangles / current_triangles
        ratio = max(0.0, min(1.0, ratio))

        modifier = target_obj.modifiers.new(name="ModelTools_Decimate", type="DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.ratio = ratio
        modifier.use_collapse_triangulate = True

        # 应用 Modifier 之前，目标物体必须是激活对象
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = target_obj

        try:
            result = bpy.ops.object.modifier_apply(modifier=modifier.name)
        except RuntimeError as err:
            self.report({"ERROR"}, "减面失败：{0}".format(err))
            context.view_layer.objects.active = prev_active
            return {"CANCELLED"}

        context.view_layer.objects.active = prev_active

        if result != {"FINISHED"}:
            self.report({"WARNING"}, "减面未完成")
            return {"CANCELLED"}

        actual_triangles = get_triangle_count(target_obj.data)

        # 恢复选择状态：保留原选中逻辑，只是把结果对象设为激活
        bpy.ops.object.select_all(action="DESELECT")
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj

        scene.modeltools_current_triangles = actual_triangles

        self.report(
            {"INFO"},
            "减面完成 原始三角面数：{0} 目标三角面数：{1} 实际三角面数：{2}".format(
                current_triangles, target_triangles, actual_triangles
            ),
        )
        return {"FINISHED"}


# -----------------------------------------------------------------------------
# Operator: 清理隐藏权重点
# -----------------------------------------------------------------------------
class MODELTOOLS_OT_clean_vertex_weights(bpy.types.Operator):
    """清理当前选中 Mesh 的顶点组中，权重小于等于 0.003 的微小权重。
    仅调用官方 Operator bpy.ops.object.vertex_group_clean，不改变顶点/面/UV/材质/骨骼/动画。
    """
    bl_idname = "modeltools.clean_vertex_weights"
    bl_label = "清理隐藏权重点"
    bl_description = "清除当前选中 Mesh 顶点组中权重 <= 0.003 的微小权重"
    bl_options = {"REGISTER", "UNDO"}

    # 权重清理阈值，固定为 0.003。
    # 声明为 Operator 属性（而非普通常量）是为了让 Blender 左下角
    # 「调整上一次操作」(Adjust Last Operation) 面板里能显示 Limit = 0.003，
    # 方便用户直接确认阈值确实生效，而不是把它当作可自由调节的参数。
    limit: bpy.props.FloatProperty(
        name="Limit",
        description="移除权重低于或等于此限值的顶点",
        default=0.003,
        min=0.0,
        max=1.0,
        precision=4,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        obj = context.active_object

        if obj is None or obj.type != "MESH":
            self.report({"WARNING"}, "请先选中一个 Mesh 对象")
            return {"CANCELLED"}

        if len(obj.vertex_groups) == 0:
            self.report({"INFO"}, "当前模型没有顶点组")
            return {"CANCELLED"}

        # 每次执行都强制回到固定阈值 0.003，避免用户在 redo 面板中意外改动后被记住
        self.limit = 0.003

        # 必须在物体模式下执行，避免影响编辑模式的选择状态
        prev_mode = obj.mode
        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        # 直接调用官方 Operator：清除顶点组权重
        # group_select_mode='ALL'  -> 处理全部顶点组
        # limit=0.003              -> 移除权重 <= 0.003 的分配（阈值，而非清零整组）
        # keep_single=False        -> 不强制为每个顶点保留至少一个组
        result = bpy.ops.object.vertex_group_clean(
            group_select_mode="ALL",
            limit=self.limit,
            keep_single=False,
        )

        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=prev_mode)

        if result == {"FINISHED"}:
            self.report({"INFO"}, "隐藏权重清理完成")
            return {"FINISHED"}

        self.report({"WARNING"}, "权重清理未执行成功")
        return {"CANCELLED"}


# -----------------------------------------------------------------------------
# Operator: 变换归零 (Transforms to Deltas - 全部变换 -> 增量)
# -----------------------------------------------------------------------------
class MODELTOOLS_OT_reset_transform(bpy.types.Operator):
    """对当前选中对象执行官方 全部变换 -> 增量（等效 Ctrl+A -> 应用 -> 全部变换 -> 增量）。

    使用 bpy.ops.object.transforms_to_deltas(mode='ALL', reset_values=True)，
    把 Location/Rotation/Scale 的数值转移到 delta_location/delta_rotation/
    delta_scale 上，普通 Transform 数值归零，但物体在世界空间中的最终变换结果
    (delta * 普通变换) 保持不变，物体不会在场景中发生视觉上的移动/旋转/缩放。
    这与直接烘焙进几何数据的 Apply Transform 不同：不修改顶点坐标或骨骼数据。
    支持同时选中 Mesh、Armature，或二者混合选择。
    """
    bl_idname = "modeltools.reset_transform"
    bl_label = "变换归零"
    bl_description = "将选中 Mesh/Armature 的 Location/Rotation/Scale 转为增量变换并归零显示数值，物体不会移动"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        selected = context.selected_objects
        if not selected:
            return False
        return all(obj.type in {"MESH", "ARMATURE"} for obj in selected)

    def execute(self, context):
        selected = context.selected_objects

        if not selected:
            self.report({"WARNING"}, "请先选中要归零变换的模型或骨骼")
            return {"CANCELLED"}

        invalid = [obj for obj in selected if obj.type not in {"MESH", "ARMATURE"}]
        if invalid:
            self.report({"WARNING"}, "所选对象中包含不支持的类型，仅支持 Mesh / Armature")
            return {"CANCELLED"}

        prev_mode = context.active_object.mode if context.active_object else "OBJECT"
        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        try:
            result = bpy.ops.object.transforms_to_deltas(mode="ALL", reset_values=True)
        except RuntimeError as err:
            self.report({"ERROR"}, "变换归零失败：{0}".format(err))
            if prev_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=prev_mode)
            return {"CANCELLED"}

        if prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=prev_mode)

        if result == {"FINISHED"}:
            self.report({"INFO"}, "变换归零完成")
            return {"FINISHED"}

        self.report({"WARNING"}, "变换归零未完成")
        return {"CANCELLED"}


# -----------------------------------------------------------------------------
# Operator: FBX 快速导出
# -----------------------------------------------------------------------------
class MODELTOOLS_OT_export_fbx(bpy.types.Operator):
    """使用固定参数导出当前选中对象为 FBX，仅弹出文件保存路径选择框。"""
    bl_idname = "modeltools.export_fbx"
    bl_label = "导出 FBX"
    bl_description = "使用固定参数将当前选中对象导出为 FBX"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={"HIDDEN"})
    check_existing: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0

    def invoke(self, context, event):
        if not context.selected_objects:
            self.report({"WARNING"}, "请先选择要导出的模型")
            return {"CANCELLED"}

        # 默认文件名使用当前活动对象名称
        base_name = context.active_object.name if context.active_object else "export"
        self.filepath = base_name + ".fbx"

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not context.selected_objects:
            self.report({"WARNING"}, "请先选择要导出的模型")
            return {"CANCELLED"}

        filepath = self.filepath
        if not filepath.lower().endswith(".fbx"):
            filepath += ".fbx"

        # 固定 FBX 导出参数，用户不可在 UI 中修改
        result = bpy.ops.export_scene.fbx(
            filepath=filepath,
            check_existing=self.check_existing,
            # Path / Batch
            path_mode="RELATIVE",
            batch_mode="OFF",
            # Include
            use_selection=True,
            use_visible=False,
            use_active_collection=False,
            # Object Types
            object_types={"EMPTY", "CAMERA", "LIGHT", "ARMATURE", "MESH", "OTHER"},
            # Transform
            global_scale=0.01,
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_ALL",
            axis_forward="Y",
            axis_up="Z",
        )

        if result == {"FINISHED"}:
            self.report({"INFO"}, "FBX 导出完成")
            return {"FINISHED"}

        self.report({"WARNING"}, "FBX 导出未完成")
        return {"CANCELLED"}


# -----------------------------------------------------------------------------
# Panel: 模型工具面板 (3D Viewport > N 面板)
# -----------------------------------------------------------------------------
class MODELTOOLS_PT_panel(bpy.types.Panel):
    bl_label = "模型工具"
    bl_idname = "MODELTOOLS_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "模型工具"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        scene = context.scene

        if obj is None or obj.type != "MESH":
            layout.label(text="请先选中一个 Mesh 对象", icon="ERROR")

        # ---------------- 模型减面 ----------------
        box = layout.box()
        box.label(text="模型减面")

        if obj is not None and obj.type == "MESH":
            current_triangles = get_triangle_count(obj.data)
        else:
            current_triangles = 0
        box.label(text="当前三角面数：{0}".format(current_triangles))

        box.prop(scene, "modeltools_target_triangles", text="目标三角面数")
        box.prop(scene, "modeltools_keep_original", text="保留原模型")

        row = box.row()
        row.scale_y = 1.4
        row.operator(
            MODELTOOLS_OT_decimate.bl_idname,
            text="开始减面",
            icon="MOD_DECIM",
        )

        layout.separator()

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator(
            MODELTOOLS_OT_clean_vertex_weights.bl_idname,
            text="清理隐藏权重点",
            icon="GROUP_VERTEX",
        )

        layout.separator()

        col3 = layout.column(align=True)
        col3.scale_y = 1.4
        col3.operator(
            MODELTOOLS_OT_reset_transform.bl_idname,
            text="变换归零",
            icon="EMPTY_AXIS",
        )

        layout.separator()

        col2 = layout.column(align=True)
        col2.scale_y = 1.4
        col2.operator(
            MODELTOOLS_OT_export_fbx.bl_idname,
            text="导出 FBX",
            icon="EXPORT",
        )


# -----------------------------------------------------------------------------
# 注册
# -----------------------------------------------------------------------------
classes = (
    MODELTOOLS_OT_decimate,
    MODELTOOLS_OT_clean_vertex_weights,
    MODELTOOLS_OT_reset_transform,
    MODELTOOLS_OT_export_fbx,
    MODELTOOLS_PT_panel,
)


def register():
    bpy.types.Scene.modeltools_target_triangles = bpy.props.IntProperty(
        name="目标三角面数",
        description="减面操作的目标三角面数量",
        default=1800,
        min=1,
        max=10000000,
    )
    bpy.types.Scene.modeltools_current_triangles = bpy.props.IntProperty(
        name="当前三角面数",
        description="上一次减面操作后的实际三角面数（仅用于显示）",
        default=0,
    )
    bpy.types.Scene.modeltools_keep_original = bpy.props.BoolProperty(
        name="保留原模型",
        description="开启后减面将在原模型的副本上进行，原模型保持不变",
        default=False,
    )
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.modeltools_target_triangles
    del bpy.types.Scene.modeltools_current_triangles
    del bpy.types.Scene.modeltools_keep_original


if __name__ == "__main__":
    register()
