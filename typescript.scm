; Definitions and imports for TypeScript / TSX extraction.
(function_declaration name: (identifier) @name) @definition.function
(generator_function_declaration name: (identifier) @name) @definition.function
(function_signature name: (identifier) @name) @definition.function
(method_definition name: (property_identifier) @name) @definition.method
(method_definition name: (private_property_identifier) @name) @definition.method
(method_signature name: (property_identifier) @name) @definition.method
(abstract_method_signature name: (property_identifier) @name) @definition.method
; Getter / setter accessor methods
(method_definition
  name: (property_identifier) @name
  (accessor_modifier)) @definition.method
; Class, interface, enum, type alias
(class_declaration name: (type_identifier) @name) @definition.class
(abstract_class_declaration name: (type_identifier) @name) @definition.class
(interface_declaration name: (type_identifier) @name) @definition.interface
(enum_declaration name: (identifier) @name) @definition.enum
(type_alias_declaration name: (type_identifier) @name) @definition.type_alias
; Arrow functions and function expressions assigned to variables.
; Covers: const login = async () => {}, const forgetProject = () => {}
(lexical_declaration
  (variable_declarator
    name: (identifier) @name
    value: [(arrow_function) (function_expression)])) @definition.function
(variable_declaration
  (variable_declarator
    name: (identifier) @name
    value: [(arrow_function) (function_expression)])) @definition.function
; Named exports: export function foo() {}, export class Foo {}
(export_statement
  declaration: (function_declaration name: (identifier) @name)) @definition.function
(export_statement
  declaration: (class_declaration name: (type_identifier) @name)) @definition.class
(export_statement
  declaration: (interface_declaration name: (type_identifier) @name)) @definition.interface
(export_statement
  declaration: (enum_declaration name: (identifier) @name)) @definition.enum
(export_statement
  declaration: (type_alias_declaration name: (type_identifier) @name)) @definition.type_alias
; Default exports: export default function App() {}, export default class App {}
; tree-sitter puts the declaration in the "value" field for default exports.
(export_statement
  value: (function_declaration name: (identifier) @name)) @definition.function
(export_statement
  value: (class_declaration name: (type_identifier) @name)) @definition.class
; Imports (including re-exports)
(import_statement) @definition.import
(export_statement
  source: (string)
  (export_clause)) @definition.import